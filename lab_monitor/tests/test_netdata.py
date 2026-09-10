"""Netdata adapter: json2 parsing, availability, StatsD emitter.

Fixtures match the real ``/api/v3/data?format=json2`` payload:
``result.labels`` with ``"time"`` first, ``result.point`` as an index map,
and points shaped ``[value, anomaly_rate, annotation]`` where bit 0 of the
annotation means empty.
"""

from __future__ import annotations

import urllib.parse

import pytest
from conftest import make_config

from lab_monitor.models import Machine
from lab_monitor.netdata import (
    GPU_METRICS,
    HEARTBEAT_CONTEXT,
    NetdataClient,
    NetdataError,
    StatsdEmitter,
    collect_machine,
    dimension_labels,
    last_entry,
    latest_values,
)

NOW = 1_700_000_000.0

GPU2 = Machine(id="gpu2", name="gpu2", room="a", netdata_hostname="gpu2")


def json2(dimensions, values, last=NOW, labels=None, timestamp=None):
    """Build a json2 response. ``None`` in ``values`` means an empty point."""
    points = []
    for value in values:
        if value is None:
            points.append([None, 0.0, 1])   # bit 0 = EMPTY
        else:
            points.append([value, 0.0, 0])

    payload = {
        "api": 3,
        "db": {"last_entry": last, "update_every": 1},
        "view": {"dimensions": {"ids": list(dimensions)}},
        "result": {
            "labels": ["time"] + list(dimensions),
            "point": {"value": 0, "arp": 1, "pa": 2},
            "data": [[timestamp or int(last)] + points],
        },
    }
    if labels:
        payload["view"]["dimensions"]["labels"] = labels
    return payload


class FakeAgent:
    """Answers data queries from a ``{(hostname, context): payload}`` map."""

    def __init__(self, responses, default=None):
        self.responses = responses
        self.default = default
        self.requests = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.requests.append((parsed.path, params))
        key = (params.get("scope_nodes"), params.get("scope_contexts"))
        if key in self.responses:
            payload = self.responses[key]
            if callable(payload):
                return payload(params)
            return payload
        if self.default is not None:
            return self.default
        raise NetdataError("no fixture for %s" % (key,))


def client(responses, default=None):
    agent = FakeAgent(responses, default)
    return NetdataClient("http://127.0.0.1:19999", fetch=agent), agent


def healthy_gpu_responses(hostname="gpu2", last=NOW, indexes=("0",), temperature=73.0):
    """A full set of context responses for a machine with the given GPUs."""
    count = len(indexes)
    return {
        (hostname, HEARTBEAT_CONTEXT): json2(["cpu"], [12.0], last=last),
        (hostname, "nvidia_smi.gpu_temperature"): json2(
            indexes, [temperature] * count, last=last
        ),
        (hostname, "nvidia_smi.gpu_utilization"): json2(indexes, [96.0] * count, last=last),
        (hostname, "nvidia_smi.gpu_fan_speed_perc"): json2(indexes, [71.0] * count, last=last),
        (hostname, "nvidia_smi.gpu_power_draw"): json2(indexes, [382.0] * count, last=last),
        (hostname, "nvidia_smi.gpu_frame_buffer_memory_usage"): lambda params: json2(
            indexes,
            [19_542_601_728.0 if params.get("dimensions") == "used" else 25_769_803_776.0] * count,
            last=last,
        ),
    }


# -- json2 parsing --------------------------------------------------------


def test_latest_values_reads_the_newest_row():
    payload = json2(["0", "1"], [70.0, 65.0])
    assert latest_values(payload) == {"0": 70.0, "1": 65.0}


def test_empty_points_are_dropped_not_reported_as_zero():
    payload = json2(["0", "1"], [70.0, None])
    values = latest_values(payload)
    assert values == {"0": 70.0}
    assert "1" not in values


def test_a_trimmed_final_row_falls_back_to_the_previous_one():
    """Netdata trims partial trailing points; we look further back."""
    payload = json2(["0"], [70.0])
    payload["result"]["data"] = [
        [int(NOW) - 1, [70.0, 0.0, 0]],
        [int(NOW), [None, 0.0, 1]],
    ]
    assert latest_values(payload) == {"0": 70.0}


def test_scalar_points_are_accepted():
    """Older/plainer formats put the value directly in the row."""
    payload = json2(["0"], [70.0])
    payload["result"]["data"] = [[int(NOW), 70.0]]
    assert latest_values(payload) == {"0": 70.0}


@pytest.mark.parametrize(
    "payload",
    [{}, {"result": None}, {"result": {}}, {"result": {"labels": ["time"], "data": []}}],
)
def test_unusable_payloads_yield_nothing_rather_than_raising(payload):
    assert latest_values(payload) == {}


def test_last_entry_is_read_from_the_db_block():
    assert last_entry(json2(["0"], [1.0], last=12345.0)) == 12345.0


@pytest.mark.parametrize("payload", [{}, {"db": {}}, {"db": {"last_entry": "soon"}}])
def test_missing_last_entry_is_none(payload):
    assert last_entry(payload) is None


def test_dimension_labels_are_extracted_when_present():
    payload = json2(
        ["0", "1"],
        [70.0, 65.0],
        labels={"product_name": [["NVIDIA RTX A5000"], ["NVIDIA RTX A4000"]]},
    )
    assert dimension_labels(payload, "product_name") == {
        "0": "NVIDIA RTX A5000",
        "1": "NVIDIA RTX A4000",
    }


def test_absent_dimension_labels_are_simply_empty():
    assert dimension_labels(json2(["0"], [70.0]), "product_name") == {}


# -- query construction ---------------------------------------------------


def test_queries_use_the_v3_data_endpoint_scoped_to_one_node():
    api, agent = client({}, default=json2(["0"], [70.0]))
    api.data("gpu2", "nvidia_smi.gpu_temperature", 180)

    path, params = agent.requests[0]
    assert path == "/api/v3/data"
    assert params["scope_nodes"] == "gpu2"
    assert params["scope_contexts"] == "nvidia_smi.gpu_temperature"
    assert params["group_by"] == "label"
    assert params["group_by_label"] == "index"
    assert params["format"] == "json2"
    assert params["points"] == "1"
    assert params["after"] == "-180"
    assert params["before"] == "0"
    assert "group-by-labels" in params["options"]


def test_vram_used_is_queried_by_filtering_the_dimension():
    api, agent = client({}, default=json2(["0"], [1.0]))
    api.data("gpu2", "nvidia_smi.gpu_frame_buffer_memory_usage", 180, "sum", "used")
    assert agent.requests[0][1]["dimensions"] == "used"
    assert agent.requests[0][1]["aggregation"] == "sum"


def test_the_contexts_we_query_are_the_documented_nvidia_smi_ones():
    contexts = {context for _, context, _, _ in GPU_METRICS}
    assert contexts == {
        "nvidia_smi.gpu_temperature",
        "nvidia_smi.gpu_utilization",
        "nvidia_smi.gpu_fan_speed_perc",
        "nvidia_smi.gpu_power_draw",
        "nvidia_smi.gpu_frame_buffer_memory_usage",
    }


# -- collect_machine ------------------------------------------------------


def test_a_fresh_machine_reports_every_gpu_metric():
    api, _ = client(healthy_gpu_responses())
    reading = collect_machine(api, GPU2, NOW, 180)

    assert reading.available is True
    assert reading.last_seen == NOW
    assert len(reading.gpus) == 1
    card = reading.gpus[0]
    assert card.index == "0"
    assert card.temperature_c == 73.0
    assert card.utilization_pct == 96.0
    assert card.fan_speed_pct == 71.0
    assert card.power_w == 382.0
    assert round(card.vram_used_bytes / 1024 ** 3, 1) == 18.2
    assert round(card.vram_total_bytes / 1024 ** 3, 0) == 24.0


def test_multiple_gpus_are_discovered_automatically():
    api, _ = client(healthy_gpu_responses(indexes=("0", "1", "2")))
    reading = collect_machine(api, GPU2, NOW, 180)
    assert [g.index for g in reading.gpus] == ["0", "1", "2"]


def test_gpu_indexes_sort_numerically_not_lexically():
    api, _ = client(healthy_gpu_responses(indexes=("0", "2", "10")))
    reading = collect_machine(api, GPU2, NOW, 180)
    assert [g.index for g in reading.gpus] == ["0", "2", "10"]


# -- case 13: a missing metric must not fail the snapshot -------------------


def test_a_context_that_is_absent_costs_only_that_field():
    responses = healthy_gpu_responses()
    del responses[("gpu2", "nvidia_smi.gpu_fan_speed_perc")]
    api, _ = client(responses)

    reading = collect_machine(api, GPU2, NOW, 180)
    assert reading.available is True
    assert reading.gpus[0].fan_speed_pct is None
    assert reading.gpus[0].temperature_c == 73.0


def test_a_context_that_returns_an_empty_point_costs_only_that_field():
    responses = healthy_gpu_responses()
    responses[("gpu2", "nvidia_smi.gpu_fan_speed_perc")] = json2(["0"], [None])
    api, _ = client(responses)

    reading = collect_machine(api, GPU2, NOW, 180)
    assert reading.gpus[0].fan_speed_pct is None
    assert reading.gpus[0].power_w == 382.0


def test_a_machine_with_no_gpu_contexts_is_still_available():
    api, _ = client({("gpu2", HEARTBEAT_CONTEXT): json2(["cpu"], [12.0])})
    reading = collect_machine(api, GPU2, NOW, 180)
    assert reading.available is True
    assert reading.gpus == ()


def test_product_names_are_picked_up_when_the_agent_supplies_them():
    responses = healthy_gpu_responses()
    responses[("gpu2", "nvidia_smi.gpu_temperature")] = json2(
        ["0"], [73.0], labels={"product_name": [["NVIDIA RTX A5000"]]}
    )
    api, _ = client(responses)
    assert collect_machine(api, GPU2, NOW, 180).gpus[0].product_name == "NVIDIA RTX A5000"


# -- availability ----------------------------------------------------------


def test_stale_data_makes_a_machine_unavailable_however_good_the_values_look():
    """The stored numbers are fine; the machine still died 10 minutes ago."""
    api, agent = client(healthy_gpu_responses(last=NOW - 600))
    reading = collect_machine(api, GPU2, NOW, 180)

    assert reading.available is False
    assert reading.gpus == ()
    assert reading.last_seen == NOW - 600
    # We stop after the heartbeat rather than querying GPU contexts pointlessly.
    assert len(agent.requests) == 1


def test_data_just_inside_the_timeout_is_still_available():
    api, _ = client(healthy_gpu_responses(last=NOW - 179))
    assert collect_machine(api, GPU2, NOW, 180).available is True


def test_a_machine_netdata_has_never_heard_of_is_unavailable():
    api, _ = client({("gpu2", HEARTBEAT_CONTEXT): {"db": {}, "result": {}}})
    reading = collect_machine(api, GPU2, NOW, 180)
    assert reading.available is False
    assert "no data in Netdata" in reading.error


def test_an_unreachable_netdata_makes_machines_unavailable_not_crash():
    def boom(url):
        raise NetdataError("connection refused")

    api = NetdataClient("http://127.0.0.1:19999", fetch=boom)
    reading = collect_machine(api, GPU2, NOW, 180)
    assert reading.available is False
    assert "connection refused" in reading.error


def test_a_non_json_response_becomes_a_netdata_error():
    api = NetdataClient("http://127.0.0.1:19999", fetch=lambda url: "<html>")
    with pytest.raises(NetdataError):
        api.get("/api/v3/data")


def test_nodes_endpoint_returns_the_node_list():
    payload = {"nodes": [{"nm": "gpu2", "state": "reachable"}]}
    api = NetdataClient("http://127.0.0.1:19999", fetch=lambda url: payload)
    assert api.nodes() == [{"nm": "gpu2", "state": "reachable"}]


def test_nodes_endpoint_tolerates_a_missing_list():
    api = NetdataClient("http://127.0.0.1:19999", fetch=lambda url: {})
    assert api.nodes() == []


# -- statsd export --------------------------------------------------------


def test_room_readings_are_sent_as_gauges():
    sent = []
    emitter = StatsdEmitter(prefix="labmonitor", send=sent.append)
    emitter.room_reading("a.3201a", temperature_c=24.5, humidity_pct=41.0, battery_pct=87.0)

    assert sent == [
        "labmonitor.room.a.3201a.temperature_c:24.5|g",
        "labmonitor.room.a.3201a.humidity_pct:41|g",
        "labmonitor.room.a.3201a.battery_pct:87|g",
    ]


def test_absent_metrics_are_simply_not_sent():
    sent = []
    StatsdEmitter(send=sent.append).room_reading("a.3201a", temperature_c=24.5)
    assert sent == ["labmonitor.room.a.3201a.temperature_c:24.5|g"]


def test_the_emitter_can_be_disabled():
    sent = []
    emitter = StatsdEmitter(enabled=False, send=sent.append)
    assert emitter.room_reading("a.3201a", temperature_c=24.5) is False
    assert sent == []


def test_a_failing_socket_is_swallowed_and_recorded():
    def boom(_line):
        raise OSError("network unreachable")

    emitter = StatsdEmitter(send=boom)
    assert emitter.gauge("room.a.temperature_c", 24.5) is False
    assert "network unreachable" in emitter.last_error


def test_metric_names_carry_room_and_sensor_so_topology_is_visible():
    """The statsd.d pattern charts match on labmonitor.room.*, so new rooms
    and sensors appear in Netdata without editing its config."""
    sent = []
    config = make_config(
        sensors=[{"id": "3201a", "name": "A", "room": "a", "address": "AA:01"}]
    )
    sensor = config.sensors[0]
    StatsdEmitter(send=sent.append).room_reading(
        "%s.%s" % (sensor.room, sensor.id), temperature_c=24.0
    )
    assert sent == ["labmonitor.room.a.3201a.temperature_c:24|g"]

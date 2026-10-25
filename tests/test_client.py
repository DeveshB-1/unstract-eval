import httpx
import pytest

from unstract_eval.client import DeploymentClient, UnstractError, extract_cost, extract_output
from unstract_eval.mock import MockUnstract

URL = "https://mock.unstract.local/deployment/api/org/invoices/"
FIXTURE = {
    "default_profile": "p1",
    "cost_per_doc": {"p1": 0.002},
    "profiles": {"p1": {"a.txt": {"total": 10}}, "p2": {"a.txt": {"total": 11}}},
}


def make(tmp_path, **kw):
    doc = tmp_path / "a.txt"
    doc.write_text("invoice")
    mock = MockUnstract(FIXTURE, **kw)
    client = DeploymentClient(URL, "mock-key", transport=mock.transport(), sleep=lambda s: None)
    return mock, client, doc


def test_extract_polls_until_done_and_reads_cost(tmp_path):
    mock, client, doc = make(tmp_path, running_polls=2)
    ex = client.extract(doc)
    assert ex.output == {"total": 10}
    assert ex.cost == 0.002
    methods = [r.method for r in mock.requests]
    assert methods == ["POST", "GET", "GET", "GET"]  # 422, 422, 200
    post = mock.requests[0]
    assert post.headers["authorization"] == "Bearer mock-key"
    assert b'name="timeout"' in post.content


def test_llm_profile_id_is_sent(tmp_path):
    mock, client, doc = make(tmp_path)
    assert client.extract(doc, llm_profile_id="p2").output == {"total": 11}
    assert b'name="llm_profile_id"\r\n\r\np2' in mock.requests[0].content


def test_bad_key_raises(tmp_path):
    mock, _, doc = make(tmp_path)
    client = DeploymentClient(URL, "wrong", transport=mock.transport(), sleep=lambda s: None)
    with pytest.raises(UnstractError, match="401"):
        client.extract(doc)


def test_consumed_result_406_raises():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"message": {"execution_id": "e1", "execution_status": "PENDING"}})
        return httpx.Response(406, json={"status": "ACKNOWLEDGED"})

    client = DeploymentClient(URL, "k", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(UnstractError, match="one-shot"):
        client._wait({"execution_id": "e1"})


def test_extract_output_accepts_documented_shapes():
    assert extract_output({"result": {"output": {"a": 1}, "metadata": {}}}) == {"a": 1}
    assert extract_output({"result": {"input_file": "x.pdf", "result": '{"a": 1}'}}) == {"a": 1}
    assert extract_output({"result": {"input_file": "x.pdf", "result": {"output": {"a": 1}}}}) == {"a": 1}
    assert extract_output({"result": '{"output": {"a": 2}}'}) == {"a": 2}
    assert extract_output({"result": None}) is None


def test_extract_cost_sums_nested_entries():
    fr = {"result": {"metadata": {"extraction_llm": [{"cost_in_dollars": "0.001"}, {"cost_in_dollars": 0.002}]}}}
    assert extract_cost(fr) == 0.003
    assert extract_cost({"result": {}}) is None

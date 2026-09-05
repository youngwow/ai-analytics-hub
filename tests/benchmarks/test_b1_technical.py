import httpx

from benchmarks.b1_technical.run_full import (
    ReplayTransport,
    decoded_headers,
    percentile,
    request_bytes,
    safe_headers,
)


def test_percentile_uses_nearest_rank():
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5


def test_safe_headers_remove_secrets():
    headers = httpx.Headers({"Authorization": "secret", "Cookie": "x", "Accept": "json"})
    assert safe_headers(headers) == {"accept": "json"}


def test_decoded_headers_remove_encoding_and_length():
    assert decoded_headers({"content-encoding": "gzip", "content-length": "10", "x": "y"}) == {
        "x": "y"
    }


def test_request_bytes_reads_stream():
    request = httpx.Request("POST", "https://example.test", content=iter([b"a", b"b"]))
    assert request_bytes(request) == b"ab"


def test_replay_miss_is_explicit(tmp_path):
    transport = ReplayTransport(tmp_path, [])
    request = httpx.Request("GET", "https://example.test/missing")
    response = transport.handle_request(request)
    assert response.status_code == 599
    assert transport.misses == 1

"""R08：冻结基线被手改/无法解析时，禁止 --append 把篡改条目重新签名。"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import freshness as f  # noqa: E402
import freshness_baseline as fb  # noqa: E402


def _write(path, entries, digest=None):
    payload = {
        "schema": 1, "kind": f.BASELINE_KIND,
        "entries": entries,
        "entries_digest": digest if digest is not None else
        f.baseline_entries_digest(entries),
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp)
    return payload


def test_append_refused_when_digest_tampered(tmp_path):
    out = tmp_path / "baseline.json"
    tampered = [{"job": "D:/x/cover_job", "version": "a", "size_bytes": 1}]
    _write(out, tampered, digest="sha256:deadbeef")
    before = out.read_bytes()

    rc = fb.main(["--root", str(tmp_path), "--out", str(out),
                  "--append", "--write"])

    assert rc == 5
    assert out.read_bytes() == before


def test_append_refused_when_file_unparsable(tmp_path):
    out = tmp_path / "baseline.json"
    out.write_bytes(b"{not json")
    rc = fb.main(["--root", str(tmp_path), "--out", str(out),
                  "--append", "--write"])
    assert rc == 5
    assert out.read_bytes() == b"{not json"


def test_force_can_rebuild_invalid_baseline(tmp_path):
    out = tmp_path / "baseline.json"
    _write(out, [{"job": "D:/x/cover_job", "version": "a", "size_bytes": 1}],
           digest="sha256:deadbeef")

    rc = fb.main(["--root", str(tmp_path), "--out", str(out), "--force", "--write"])

    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["entries"] == []
    assert data["entries_digest"] == f.baseline_entries_digest([])


def test_append_keeps_history_when_valid(tmp_path):
    out = tmp_path / "baseline.json"
    old = [{"job": "D:/x/cover_job", "version": "a", "size_bytes": 1}]
    _write(out, old)

    rc = fb.main(["--root", str(tmp_path), "--out", str(out),
                  "--append", "--write"])

    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["entries"] == old
    assert data["entries_digest"] == f.baseline_entries_digest(old)

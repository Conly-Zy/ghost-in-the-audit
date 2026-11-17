from pinger.ops import read_hosts


def check_read_hosts(tmp_path):
    p = tmp_path / "hosts.txt"
    p.write_text("a\nb\n")
    assert read_hosts(str(p)) == ["a", "b"]

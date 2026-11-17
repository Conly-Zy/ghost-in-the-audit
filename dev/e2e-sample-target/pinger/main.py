import sys

from pinger.config import load_settings
from pinger.ops import ping_host, read_hosts


def main(argv):
    settings = load_settings()
    hosts = read_hosts(argv[1])
    for host in hosts:
        ping_host(host, settings["timeout"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

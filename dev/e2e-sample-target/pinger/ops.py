import os


def read_hosts(path):
    f = open(path)
    hosts = [line.strip() for line in f]
    return hosts


def ping_host(host, timeout):
    try:
        os.system("ping -c 1 -W " + str(timeout) + " " + host)
    except Exception:
        pass

#!/usr/bin/env python3
"""Генератор nftables-конфига блок-листа для AmneziaWG."""
import argparse
import ipaddress
import json


def load_networks(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    nets = []
    for item in data:
        host = (item.get("hostname") or "").strip()
        if not host:
            continue
        if "/" not in host:
            host += "/32"
        nets.append(ipaddress.ip_network(host, strict=False))
    return nets


def merge_ranges(nets):
    """Объединяет пересекающиеся и смежные интервалы в непересекающиеся."""
    ranges = sorted(
        (int(n.network_address), int(n.broadcast_address)) for n in nets
    )
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def token(start, end):
    first = str(ipaddress.ip_address(start))
    if start == end:
        return first
    return f"{first}-{ipaddress.ip_address(end)}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("json_file", help="amnezia-ip.json")
    parser.add_argument("-o", "--output", default="nftables-awg-block.conf")
    parser.add_argument("--port", type=int, required=True, help="внутренний ListenPort AmneziaWG")
    parser.add_argument("--extra", action="append", default=[], help="доп. IP/подсеть (можно повторять)")
    parser.add_argument("--dns", action="append", default=None, help="DNS для исключения (по умолчанию 77.88.8.1, 77.88.8.8)")
    parser.add_argument("--no-dns-accept", action="store_true", help="не добавлять исключения для DNS")
    parser.add_argument("--batch", type=int, default=100, help="адресов в одной команде add element")
    args = parser.parse_args()

    nets = load_networks(args.json_file)
    for extra in args.extra:
        if "/" not in extra:
            extra += "/32"
        nets.append(ipaddress.ip_network(extra, strict=False))

    merged = merge_ranges(nets)
    tokens = [token(start, end) for start, end in merged]

    dns = args.dns if args.dns is not None else ["77.88.8.1", "77.88.8.8"]
    if args.no_dns_accept:
        dns = []

    lines = [
        "#!/usr/sbin/nft -f",
        "",
        "# идемпотентность: создаём пустышку, если таблицы нет, и удаляем её",
        "table inet awg_block",
        "delete table inet awg_block",
        "",
        "table inet awg_block {",
        "    set blocked4 {",
        "        type ipv4_addr",
        "        flags interval",
        "    }",
        "",
        "    chain forward {",
        "        type filter hook forward priority filter + 10; policy accept;",
        "",
        "        # ответный трафик AmneziaWG клиентам (source port = ListenPort)",
        f"        udp sport {args.port} counter accept",
        "        # ответы на входящие DNAT-подключения",
        "        ct status dnat counter accept",
    ]
    if dns:
        lines += [
            "        # не блокируем DNS-серверы",
            "        ip daddr { " + ", ".join(dns) + " } counter accept",
        ]
    lines += [
        "",
        "        meta l4proto tcp ip daddr @blocked4 counter reject with tcp reset",
        "        ip daddr @blocked4 counter reject with icmpx type admin-prohibited",
        "    }",
        "}",
        "",
    ]

    for i in range(0, len(tokens), args.batch):
        chunk = ", ".join(tokens[i:i + args.batch])
        lines.append(f"add element inet awg_block blocked4 {{ {chunk} }}")

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"{args.output}: {len(tokens)} непересекающихся диапазонов (из {len(nets)} префиксов)")


if __name__ == "__main__":
    main()

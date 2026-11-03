# Блокировка IP на уровне сервера для AmneziaWG (nftables)

Инструкция и готовый генератор конфигурации, которые позволяют **серверу AmneziaWG** не пускать VPN-клиентов на заданные IP-адреса и подсети. Блокировка работает на уровне ОС, клиентские приложения менять не нужно.

Решение не трогает Docker и правила Amnezia: создаётся отдельная таблица nftables `inet awg_block` и отдельный systemd-юнит. Пересоздание контейнера Amnezia, обновление конфига или добавление клиентов в приложении эту таблицу не затрагивают.

---

## Содержание

- [Как это работает](#как-это-работает)
- [Почему не iptables/UFW](#почему-не-iptablesufw)
- [Требования](#требования)
- [Шаг 0. Бэкап и сбор параметров](#шаг-0-бэкап-и-сбор-параметров)
- [Шаг 1. Генерация конфига](#шаг-1-генерация-конфига)
- [Шаг 2. Установка и запуск](#шаг-2-установка-и-запуск)
- [Шаг 3. Проверка](#шаг-3-проверка)
- [Обновление списка](#обновление-списка)
- [Откат](#откат)
- [Диагностика](#диагностика)
- [Важные предостережения](#важные-предостережения)
- [Файлы репозитория](#файлы-репозитория)

---

## Как это работает

Создаётся отдельная таблица nftables:

```nft
table inet awg_block {
    set blocked4 {
        type ipv4_addr
        flags interval
    }

    chain forward {
        type filter hook forward priority filter + 10; policy accept;

        # ответный трафик AmneziaWG клиентам (source port = ListenPort)
        # 46559 — пример, подставьте свой ListenPort
        udp sport 46559 counter accept
        # ответы на входящие DNAT-подключения
        ct status dnat counter accept
        # не блокируем DNS-серверы Яндекса (при необходимости)
        ip daddr { 77.88.8.1, 77.88.8.8 } counter accept

        meta l4proto tcp ip daddr @blocked4 counter reject with tcp reset
        ip daddr @blocked4 counter reject with icmpx type admin-prohibited
    }
}
```

Ключевые моменты:

- Хук `forward` с приоритетом `filter + 10` — правила применяются к пересылаемому трафику VPN-клиентов, а не к трафику самого сервера и не к SSH.
- `set blocked4` хранит IPv4-адреса и подсети. Обязателен `flags interval`, иначе CIDR/диапазоны не добавятся.
- **Первые два `accept`-правила критически важны.** Они пропускают обратный трафик WireGuard (handshake и шифрованные данные) и ответы на входящие DNAT-подключения. Без них `reject` по `daddr` рубит ответные пакеты клиентам, публичные IP которых (например, за NAT мобильного оператора) тоже могут попадать в блок-лист. Симптом: телефон не подключается к VPN, хотя список «всего лишь» блокирует сайты.
- `udp sport <ListenPort>` — все пакеты от AmneziaWG клиентам уходят с source port, равным внутреннему `ListenPort`. Это надёжный способ отличить ответы VPN от обычного трафика клиентов в интернет.
- `ct status dnat accept` дополнительно пропускает ответы на любые входящие DNAT-подключения (проброс портов Docker и т.п.).
- TCP-соединения к заблокированным адресам рвутся сразу (`reject with tcp reset`), остальное (UDP/QUIC/ICMP) получает `admin-prohibited`. Так клиент не висит в таймауте.
- Если нужен «тихий» дроп — замените `reject ...` на `drop`.

Схема работает и при `network=bridge`, и при `network=host` у контейнера Amnezia:

- **bridge** — трафик клиентов и обратные пакеты идут через `forward` хоста;
- **host** — handshake идёт через `input`/`output`, а пересылка клиентов — через `forward`; `accept`-правила по source port просто не срабатывают, что безопасно.

---

## Почему не iptables/UFW

- Amnezia настраивает свои правила внутри контейнера и может перетирать их при изменении настроек или добавлении клиента.
- Docker вставляет свои цепочки в `FORWARD` раньше цепочек UFW, поэтому правила UFW для пересылаемого трафика могут не работать.
- Отдельная таблица nftables не зависит от порядка инициализации Docker и Amnezia и не конфликтует с ними.
- `iptables -I FORWARD ...` может быть снесено при перезапуске Docker/Amnezia.

---

## Требования

- Ubuntu/Debian (или другой Linux с nftables) и root.
- Установленный `nft` (`apt install -y nftables`, если его нет).
- AmneziaWG, запущенный в Docker.
- Список блокируемых адресов в JSON вида:

```json
[
  { "hostname": "2.16.20.0/23", "ip": "" },
  { "hostname": "2.16.53.0/24", "ip": "" }
]
```

Поле `hostname` может содержать CIDR (`1.2.3.0/24`) или одиночный IP (`1.2.3.4`).

---

## Шаг 0. Бэкап и сбор параметров

```bash
sudo -i

# бэкап текущих правил
iptables-save > /root/iptables-$(date +%F).bak
nft list ruleset > /root/nft-$(date +%F).bak

# имя контейнера Amnezia
docker ps --format '{{.Names}}'

# режим сети контейнера
docker inspect -f 'network={{.HostConfig.NetworkMode}}' <CONTAINER>

# внутренний ListenPort и текущие endpoint клиентов
docker exec <CONTAINER> awg show 2>/dev/null || docker exec <CONTAINER> wg show

# проброшенный порт
docker port <CONTAINER>

# внешний интерфейс (для справки)
ip route get 1.1.1.1
```

Из `awg show` возьмите строку `listening port:`. Это и есть `<WG_PORT>` для конфига. В примерах ниже используется `46559` — замените на своё значение.

В строке `endpoint:` каждого пира виден публичный IP клиента. Если он попадает в блок-лист — это нормально, именно для таких случаев и нужны `accept`-правила по source port.

Проверьте, какой DNS выдаётся клиентам (в конфиге клиента в приложении Amnezia, строка `DNS = ...`). Если это `77.88.8.1` / `77.88.8.8` (Яндекс DNS), их нужно исключить из блокировки, иначе у клиентов отвалится резолвинг. Другие популярные DNS (`1.1.1.1`, `8.8.8.8`, `9.9.9.9`) в списке отсутствуют.

> На серверном `awg0.conf` строки `DNS` обычно нет — DNS задаётся в клиентском конфиге приложения.

---

## Шаг 1. Генерация конфига

Сохраните скрипт ниже как `generate-conf.py` рядом с `amnezia-ip.json`.

```python
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
```

Запуск:

```bash
python3 generate-conf.py amnezia-ip.json \
  -o nftables-awg-block.conf \
  --port 46559 \
  --extra 155.212.204.140
```

Параметры:

- `--port` — внутренний `ListenPort` из `awg show` (обязательный).
- `--extra` — дополнительный адрес/подсеть, которую нужно блокировать (необязательно, можно повторять).
- `--dns` — IP DNS, которые нужно исключить (по умолчанию `77.88.8.1` и `77.88.8.8`). Чтобы использовать другой DNS: `--dns 1.1.1.1 --dns 8.8.8.8`. Чтобы убрать исключения: `--no-dns-accept`.
- `--batch` — сколько адресов в одной `add element` (по умолчанию 100).

### Почему скрипт объединяет интервалы

У сета стоит `flags interval` без `auto-merge`. Если добавить перекрывающиеся префиксы (например, `217.107.214.0/24` и `217.107.214.240/28`), nft завершится ошибкой:

```text
Error: conflicting intervals
```

Скрипт заранее объединяет пересекающиеся и смежные интервалы в непересекающиеся диапазоны вида `2.16.20.0-2.16.21.255`. Покрытие адресов при этом не меняется, а конфиг работает на любой версии nftables.

Альтернатива: добавить в сету `flags interval, auto-merge` и не объединять адреса вручную. Но эта опция поддерживается не во всех версиях nftables, поэтому генератор использует более совместимый вариант.

---

## Шаг 2. Установка и запуск

```bash
install -m 0644 nftables-awg-block.conf /etc/nftables-awg-block.conf

cat > /etc/systemd/system/awg-block.service <<'EOF'
[Unit]
Description=Server-side IP blocklist for AmneziaWG
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables-awg-block.conf
ExecStop=/usr/sbin/nft delete table inet awg_block

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now awg-block.service
systemctl status awg-block.service --no-pager
```

`ExecStop` удаляет **только** таблицу `awg_block`, а не весь ruleset. Docker и Amnezia при остановке сервиса не затрагиваются.

> Убедитесь, что таблицы ещё нет перед первым запуском. Проверить: `nft list table inet awg_block`. Если таблица существует, удалите только её: `nft delete table inet awg_block`.

---

## Шаг 3. Проверка

На сервере:

```bash
systemctl is-active awg-block.service

nft list chain inet awg_block forward
nft list set inet awg_block blocked4 | head -5
```

Счётчики `counter` показывают, сколько пакетов прошло через каждое правило. Их значения будут расти при работе VPN и при попытках доступа к заблокированным адресам.

**Тестировать нужно с клиента VPN, а не с самого сервера.** Пакеты, которые сервер отправляет сам, идут через `output`, а правила стоят в `forward`, поэтому `ping` с сервера ничего не покажет.

С клиента (с включённым VPN):

```bash
curl -I --max-time 5 https://ifconfig.me   # остальной интернет должен работать
ping 2.16.103.1                            # Destination unreachable / timeout
curl -I --max-time 5 http://2.16.103.1     # Connection reset / refused
```

`2.16.103.1` — пример адреса из списка. Подставьте свой заблокированный адрес.

После проверки на сервере:

```bash
nft list chain inet awg_block forward
```

Счётчик `udp sport <WG_PORT> accept` должен расти при активном VPN, а счётчики `reject` — при попытках доступа к заблокированным адресам.

---

## Обновление списка

1. Обновите `amnezia-ip.json`.
2. Перегенерируйте конфиг:

```bash
python3 generate-conf.py amnezia-ip.json -o nftables-awg-block.conf --port 46559
```

3. Установите и перезапустите:

```bash
install -m 0644 nftables-awg-block.conf /etc/nftables-awg-block.conf
systemctl restart awg-block.service
```

`restart` безопасен: `ExecStop` сначала удаляет старую таблицу, затем `ExecStart` создаёт новую. Если сервис неактивен, а таблица осталась (например, применяли вручную), удалите её перед запуском:

```bash
nft delete table inet awg_block
systemctl start awg-block.service
```

### Добавление/удаление адреса на живую

```bash
nft add element inet awg_block blocked4 { 203.0.113.77 }
nft delete element inet awg_block blocked4 { 203.0.113.77 }
```

Изменения применятся сразу, без разрыва VPN. Чтобы они сохранились после перезагрузки, добавьте адрес в `amnezia-ip.json` (или через `--extra`) и перегенерируйте конфиг.

---

## Откат

Полностью отключить блокировку (Docker/Amnezia не пострадают):

```bash
systemctl disable --now awg-block.service
```

Если таблица почему-то осталась:

```bash
nft delete table inet awg_block
```

---

## Диагностика

### Сервис не запускается

```bash
journalctl -xeu awg-block.service -n 50 --no-pager
```

Типовые причины:

- **`Error: conflicting intervals`** — в конфиге перекрывающиеся префиксы. Используйте генератор из этой инструкции (он объединяет интервалы) или добавьте сету `flags interval, auto-merge`.
- **`File exists`** — таблица уже существует. Удалите её: `nft delete table inet awg_block`, затем запустите сервис.
- **`No such file or directory`** — не установлен `nft` или неверный путь в `ExecStart`.

Если `nft -f` завершился ошибкой, таблица не создаётся, поэтому VPN продолжает работать. Это безопасное состояние.

### Клиенты не подключаются к VPN после включения блокировки

Проверьте, что в `chain forward` есть правила:

```nft
udp sport <WG_PORT> counter accept
ct status dnat counter accept
```

И что `<WG_PORT>` совпадает с `listening port` из `awg show`. Если порт Amnezia изменился, обновите конфиг и перезапустите сервис.

Убедитесь, что публичный IP клиента (из `endpoint:` в `awg show`) может попадать в блок-лист — именно поэтому `accept` по source port обязателен.

### Сайты не блокируются

- Проверьте, что клиент действительно ходит через VPN (split tunneling в приложении может пускать часть трафика мимо).
- Проверьте, что адрес входит в сет: `nft get element inet awg_block blocked4 { 203.0.113.77 }`.
- Проверьте, что блокируется нужное направление: правила в `forward` блокируют трафик клиентов, но не трафик самого сервера (для сервера нужна отдельная цепочка `output`).
- Если сайт доступен по IPv6, а в туннеле есть IPv6 — нужен отдельный `set blocked6` и правило `ip6 daddr @blocked6 reject`.

### DNS не работает у клиентов

Если клиентам выдаётся `77.88.8.1` / `77.88.8.8`, они должны быть в `accept`-правилах (генератор добавляет их по умолчанию). Для других DNS добавьте их через `--dns` или смените DNS клиента на `1.1.1.1` / `8.8.8.8`.

---

## Важные предостережения

- **Никогда не выполняйте** на сервере с Docker/Amnezia:
  - `nft flush ruleset`
  - `iptables -F`, `iptables -X`
  - `ufw reset`
  
  Это удаляет runtime-правила Docker (NAT, DNAT, FORWARD), после чего контейнеры теряют сеть, а VPN-порт перестаёт отвечать.

- Не включайте стандартный `nftables.service`, если его `/etc/nftables.conf` начинается с `flush ruleset`: при рестарте он снесёт правила Docker/Amnezia. Используйте отдельный юнит `awg-block.service`.

- Правило действует на **весь форвардинг** хоста. Если на VPS есть другие контейнеры (Xray, прокси и т.п.), их трафик к адресам из списка тоже будет заблокирован.

- Не добавляйте в список:
  - IP самого VPS;
  - подсети Docker (`172.16.0.0/12`, `172.17.0.0/16`);
  - подсети VPN-клиентов (`10.8.0.0/24` и т.п.);
  - DNS-серверы, которые используют клиенты;
  - `0.0.0.0/0`.

- Блокировка по IP плохо работает против сайтов на CDN с динамическими адресами. Для доменов надёжнее DNS-фильтрация (AdGuard Home/dnsmasq) или периодическое обновление списка по резолву доменов.

- Список может содержать широкие диапазоны (`/13`–`/16`), в которых находятся домашние и мобильные сети клиентов. Это нормально для блокировки, но именно поэтому нельзя убирать `accept`-правила для обратного трафика WireGuard.

- Перед изменениями делайте бэкап правил (`nft list ruleset`, `iptables-save`) и держите под рукой доступ к консоли хостера.

---

## Файлы репозитория

```
.
├── README.md                    # эта инструкция
├── amnezia-ip.json              # исходный список адресов
├── generate-conf.py             # генератор конфига (код из Шага 1)
└── nftables-awg-block.conf      # готовый конфиг для /etc
```

`nftables-awg-block.conf` генерируется под конкретный сервер (порт, DNS, дополнительные адреса), поэтому при переносе на другой сервер перегенерируйте его с нужными параметрами.

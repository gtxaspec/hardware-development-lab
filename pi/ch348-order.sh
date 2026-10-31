#!/bin/bash
# Order CH348 USB serial adapters so ttyUSB numbering is stable across boots.
#
# Keyed on each adapter's platform controller plus its hub port chain. That
# pair survives USB bus renumbering (the bus number is stripped) and names a
# physical position, so it only changes when the bench is re-wired.
#
# CH348_ORDER (from /etc/default/ch348-order) lists the keys in the order they
# should take ttyUSB blocks: the first gets 0-7, the second 8-15, and so on.
# Adapters that are present but unlisted follow in sorted order, so numbering
# stays deterministic even with no config or after a re-wire. --dry-run prints
# the keys and mapping without touching the ports; use it to fill in the config.
set -u

CH348_ORDER=""
[ -r /etc/default/ch348-order ] && . /etc/default/ch348-order
ORDER=$CH348_ORDER
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

declare -A AUTH
found=()
for f in $(grep -l 1a86 /sys/bus/usb/devices/*/idVendor 2>/dev/null); do
    d=$(dirname "$f")
    [ -e "$d/authorized" ] || continue
    rp=$(readlink -f "$d")
    plat=${rp#/sys/devices/platform/}; plat=${plat%%/*}
    node=$(basename "$d")
    key="$plat:${node#*-}"
    AUTH["$key"]="$d/authorized"
    found+=("$key")
done

if [ ${#found[@]} -eq 0 ]; then
    echo "ch348-order: no CH348 adapters found, nothing to do"
    exit 0
fi

seq_keys=()
for k in $ORDER; do
    [ -n "${AUTH[$k]:-}" ] && seq_keys+=("$k")
done
for k in $(printf '%s\n' "${found[@]}" | sort -u); do
    case " ${seq_keys[*]-} " in *" $k "*) ;; *) seq_keys+=("$k") ;; esac
done

echo "ch348-order: ${#seq_keys[@]} adapter(s): ${seq_keys[*]}"
for k in $ORDER; do
    [ -z "${AUTH[$k]:-}" ] && echo "ch348-order: note: configured key $k is not present (re-wired?)"
done

if [ "$DRY" = 1 ]; then
    n=0
    for k in "${seq_keys[@]}"; do echo "ch348-order: would map $k -> ttyUSB$n-$((n+7))"; n=$((n+8)); done
    exit 0
fi

wait_for() { for i in $(seq 1 40); do [ -e "$1" ] && return 0; sleep 0.3; done; return 1; }

for k in "${seq_keys[@]}"; do echo 0 > "${AUTH[$k]}"; done
sleep 1

n=0
for k in "${seq_keys[@]}"; do
    echo 1 > "${AUTH[$k]}"
    last=$((n+7))
    wait_for "/dev/ttyUSB$last" || echo "ch348-order: warning: ttyUSB$last never appeared for $k"
    echo "ch348-order: $k -> ttyUSB$n-$last"
    n=$((n+8))
done
sleep 0.5

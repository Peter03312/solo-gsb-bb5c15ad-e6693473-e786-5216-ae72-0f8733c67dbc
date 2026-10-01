#!/usr/bin/env bash
# 冷链追责服务 —— 整车交接 调用示例（curl）。
#
# 演示：
#   登记车次与多票货物装载、提交整车交接（在车清单按左闭右开自动确定）、
#   返回交接单及逐票普通交接事件编号；空车 / 双方相同 / 保管方不符 /
#   同刻已有交接整单拒绝；同单号同内容重传幂等、异内容冲突；
#   交接单保留当时清单——事后补登记装载改变交接时刻在车集合则整批拒绝；
#   交接与测温同刻时责任自该时刻归接收方（既有追责 / 责任接口直接可用）。
#
# 用法：BASE=http://127.0.0.1:8000 bash examples/curl_handovers.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
T="TRUCK-HO-1"
C1="HANDOVER-CARGO-1"   # 上限 4℃，起始保管方 北京冷库
C2="HANDOVER-CARGO-2"   # 上限 7℃，起始保管方 北京冷库
C3="HANDOVER-CARGO-3"   # 中途才装车，12:00 不在车上

echo "1) 登记三票货物"
for C in "$C1 4.0" "$C2 7.0" "$C3 4.0"; do
  set -- $C
  curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
    \"cargo_id\": \"$1\",
    \"started_at\": \"2026-10-01T08:00:00+08:00\",
    \"temp_upper_limit\": \"$2\",
    \"initial_holder\": \"北京冷库\"
  }"; echo
done

echo "2) 登记车次"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"$T\",
  \"vehicle_no\": \"京A·H0001\",
  \"note\": \"10月1日 整车交接班次\"
}"; echo

echo "3) 登记装载时段（左闭右开）：C1/C2 全程在车上；C3 13:00 才装车"
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"$C1\",
     \"loaded_at\": \"2026-10-01T10:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T18:00:00+08:00\"},
    {\"cargo_id\": \"$C2\",
     \"loaded_at\": \"2026-10-01T10:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T18:00:00+08:00\"},
    {\"cargo_id\": \"$C3\",
     \"loaded_at\": \"2026-10-01T13:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T18:00:00+08:00\"}
  ]
}"; echo

echo "4) 11:00 车载读数 8.0℃（为 C1 开启越温区间；C2 上限 7℃ 也越温）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"ho-rd-1\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-01T11:00:00+08:00\", \"temperature\": \"8.0\"
}"; echo

echo "5) 提交整车交接 12:00 北京冷库 -> 济南冷库（清单=C1,C2；逐票产生普通交接事件）"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-20261001-1200\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T12:00:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"济南冷库\"
}" | python3 -m json.tool; echo

echo "6) 同交接单号同内容重传 -> 200 replayed，返回原交接单与原逐票事件编号，不重复产生事件"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-20261001-1200\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T12:00:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"济南冷库\"
}"; echo

echo "7) 同交接单号异内容（换接收方）-> 409 handover_id_conflict"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-20261001-1200\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T12:00:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"青岛冷库\"
}"; echo

echo "8) 非法：交出方与接收方相同 -> 422 bad_handover_shape（整单拒绝、无货物事件）"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-BAD-1\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T12:30:00+08:00\",
  \"giver\": \"济南冷库\",
  \"receiver\": \"济南冷库\"
}"; echo

echo "9) 非法：12:00 当前保管方已是济南冷库，仍以北京冷库为交出方 -> 422 giver_mismatch"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-BAD-2\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T12:30:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"青岛冷库\"
}"; echo

echo "10) 非法：同刻已有交接（12:00 再来一张，即使双方正确）-> 409 same_timestamp_conflict"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-BAD-3\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T12:00:00+08:00\",
  \"giver\": \"济南冷库\",
  \"receiver\": \"青岛冷库\"
}"; echo

echo "11) 空车：19:00 货物均已卸载 -> 422 empty_vehicle"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-BAD-4\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T19:00:00+08:00\",
  \"giver\": \"济南冷库\",
  \"receiver\": \"青岛冷库\"
}"; echo

echo "12) C3 于 13:30 经普通单货物交接 北京冷库 -> 济南冷库（C3 13:00 才装车，未参与 12:00 整车交接）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"ho-c3-h-1\", \"cargo_id\": \"$C3\",
  \"occurred_at\": \"2026-10-01T13:30:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"济南冷库\"
}"; echo
echo "    第二张合法整车交接 15:00 济南冷库 -> 青岛冷库（此时 C3 已在车上且保管方一致，清单=C1,C2,C3）"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"HO-20261001-1500\",
  \"trip_id\": \"$T\",
  \"handed_over_at\": \"2026-10-01T15:00:00+08:00\",
  \"giver\": \"济南冷库\",
  \"receiver\": \"青岛冷库\"
}" | python3 -m json.tool; echo

echo "13) 16:00 车载读数 3.0℃：闭合越温区间（区间跨 12:00 整车交接拆分责任）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"ho-rd-2\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-01T16:00:00+08:00\", \"temperature\": \"3.0\"
}"; echo

echo "14) C1 追责报告：[11,12) 北京冷库、[12,16) 济南冷库（交接与测温同刻时自交接时刻归接收方）"
curl -sS "$BASE/cargo/$C1/report" | python3 -m json.tool; echo

echo "15) 清单冻结：新增一票货并补登 [11:00,12:30)，会改变 HO-...-1200 在 12:00 的在车集合"
echo "    （新货与既有时段无任何重叠，纯粹因冻结被拒）-> 409 handover_manifest_conflict"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"HANDOVER-CARGO-5\",
  \"started_at\": \"2026-10-01T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}" >/dev/null
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"HANDOVER-CARGO-5\",
     \"loaded_at\": \"2026-10-01T11:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T12:30:00+08:00\"}
  ]
}"; echo
echo "    补登一个不覆盖任何已完成交接时刻（12:00 / 15:00）的时段 -> 201"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"HANDOVER-CARGO-4\",
  \"started_at\": \"2026-10-01T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"青岛冷库\"
}" >/dev/null
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"HANDOVER-CARGO-4\",
     \"loaded_at\": \"2026-10-01T16:30:00+08:00\",
     \"unloaded_at\": \"2026-10-01T17:30:00+08:00\"}
  ]
}"; echo

echo "16) 查询交接单：车次交接单列表 + 按单号直查（含冻结清单与逐票事件编号）"
curl -sS "$BASE/trips/$T/handovers" | python3 -m json.tool; echo
curl -sS "$BASE/handovers/HO-20261001-1200" | python3 -m json.tool; echo

echo "17) 既有单货物接口兼容：逐票交接事件可在货物事件列表中查到"
curl -sS "$BASE/cargo/$C1/events" | python3 -m json.tool; echo

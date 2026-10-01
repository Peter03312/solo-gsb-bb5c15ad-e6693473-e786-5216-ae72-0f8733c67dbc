#!/usr/bin/env bash
# 冷链追责服务 —— 整车交接 调用示例（curl）。
#
# 演示：
#   两车票货物装在同一车次上 -> 整车交接一次性交出（在车清单按左闭右开确定、
#   成功后每票货物记一笔普通交接事件、返回交接单与逐票事件编号）；
#   空车 / 双方相同 / 交出方不是保管方 / 同刻重复交接 整单拒绝；
#   同交接单号同内容重传幂等、异内容冲突；
#   交接与测温同刻时责任片段从交接时刻归接收方；
#   交接单保留当时清单，事后补登记装载若改变交接时刻在车集合则整批拒绝。
#
# 用法：BASE=http://127.0.0.1:8000 bash examples/curl_handovers.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
T="TRUCK-HANDOVER-1"
C1="VH-CARGO-1"   # 上限 4℃
C2="VH-CARGO-2"   # 上限 4℃

echo "1) 登记两票货物（起始保管方均为 北京冷库）"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C1\",
  \"started_at\": \"2026-10-02T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}"; echo
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C2\",
  \"started_at\": \"2026-10-02T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}"; echo

echo "2) 登记车次并批量登记装卸时段：C1 [09:00,18:00)；C2 [10:00,17:00)"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"$T\", \"vehicle_no\": \"京A·F0099\", \"note\": \"10月2日 京沪班次\"
}"; echo
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"$C1\",
     \"loaded_at\": \"2026-10-02T09:00:00+08:00\",
     \"unloaded_at\": \"2026-10-02T18:00:00+08:00\"},
    {\"cargo_id\": \"$C2\",
     \"loaded_at\": \"2026-10-02T10:00:00+08:00\",
     \"unloaded_at\": \"2026-10-02T17:00:00+08:00\"}
  ]
}"; echo

echo "3) 11:00 车载读数 8.0℃（两票货都在车上，按各自上限均越温，开启区间）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vh-rd-1\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-02T11:00:00+08:00\", \"temperature\": \"8.0\"
}"; echo

echo "4) 整车交接 12:00 北京冷库 -> 济南冷库（在车清单=此刻在车上的全部货物，左闭右开）"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"VH-DOC-0001\",
  \"trip_id\": \"$T\",
  \"handover_at\": \"2026-10-02T12:00:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"济南冷库\"
}" | tee /tmp/vh_resp.json | python3 -m json.tool; echo

echo "5) 同单号同内容重传 -> 200 replayed，返回原交接单与原逐票事件编号（不重复写事件）"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"VH-DOC-0001\",
  \"trip_id\": \"$T\",
  \"handover_at\": \"2026-10-02T12:00:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"济南冷库\"
}"; echo

echo "6) 同单号异内容（换了接收方）-> 409 handover_id_conflict"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"VH-DOC-0001\",
  \"trip_id\": \"$T\",
  \"handover_at\": \"2026-10-02T12:00:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"上海冷库\"
}"; echo

echo "7) 非法示例 a：同车次同刻再来一笔整车交接 -> 409 same_timestamp_conflict"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"VH-DOC-0002\",
  \"trip_id\": \"$T\",
  \"handover_at\": \"2026-10-02T12:00:00+08:00\",
  \"giver\": \"济南冷库\",
  \"receiver\": \"上海冷库\"
}"; echo

echo "8) 非法示例 b：交出方不是当前保管方（12:00 后保管方已是济南冷库）"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"VH-DOC-0003\",
  \"trip_id\": \"$T\",
  \"handover_at\": \"2026-10-02T13:00:00+08:00\",
  \"giver\": \"北京冷库\",
  \"receiver\": \"上海冷库\"
}"; echo

echo "9) 非法示例 c：交出方与接收方相同 -> 422 bad_handover_shape"
curl -sS -X POST "$BASE/trips/$T/handovers" -H 'Content-Type: application/json' -d "{
  \"handover_id\": \"VH-DOC-0004\",
  \"trip_id\": \"$T\",
  \"handover_at\": \"2026-10-02T14:00:00+08:00\",
  \"giver\": \"济南冷库\",
  \"receiver\": \"济南冷库\"
}"; echo

echo "10) 逐票货物事件查询：每票货物各有一笔 event_type=handover 的普通交接事件"
for C in "$C1" "$C2"; do
  echo "-- $C 自有事件："
  curl -sS "$BASE/cargo/$C/events" | python3 -m json.tool
done

echo "11) 14:00 车载读数 8.0℃（交接后，越温区间延续在济南冷库片段）；15:00 读数 3.0℃ 闭合"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vh-rd-2\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-02T14:00:00+08:00\", \"temperature\": \"8.0\"
}" > /dev/null
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vh-rd-3\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-02T15:00:00+08:00\", \"temperature\": \"3.0\"
}" > /dev/null
echo "    C1 追责报告（11:00 车载开启、12:00 整车交接拆分、15:00 车载闭合）："
curl -sS "$BASE/cargo/$C1/report" | python3 -m json.tool; echo

echo "12) 交接单详情 / 列表（清单为当时快照，含逐票事件编号）"
curl -sS "$BASE/handovers/VH-DOC-0001" | python3 -m json.tool; echo
curl -sS "$BASE/handovers?trip_id=$T" | python3 -m json.tool; echo

echo "13) 清单冻结：给新货物补登记覆盖 12:00 交接时刻的装载 -> 409 loading_manifest_frozen"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"VH-CARGO-3\",
  \"started_at\": \"2026-10-02T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}" > /dev/null
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"VH-CARGO-3\",
     \"loaded_at\": \"2026-10-02T11:30:00+08:00\",
     \"unloaded_at\": \"2026-10-02T12:30:00+08:00\"}
  ]
}"; echo

echo "14) 不覆盖交接时刻的补登记允许（16:00 之后才装车）"
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"VH-CARGO-3\",
     \"loaded_at\": \"2026-10-02T16:00:00+08:00\",
     \"unloaded_at\": \"2026-10-02T16:30:00+08:00\"}
  ]
}"; echo

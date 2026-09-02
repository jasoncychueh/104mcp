"""Two zero-request, zero-quota discovery tools: `browse_filter_values` (what values a
`search_resumes` `filters` key takes) and `describe_result_fields` (what the 35 fields on
a résumé row mean).

Answers: given a `filters` key from `tools/filters.py`'s `CONDITIONS`, what can go in it —
uniformly, whether that key is backed by a 6,000-node category tree, a small labelled
enum, a composite structure, or nothing browsable at all; and given the fixed set of
fields `tools/search.py` keeps on a résumé row, what does each one mean.

Deliberately does not: touch the network, the session, the guard or the throttle —
`structure.md` already rules that public category data must not be routed through
`guarded_api` or the throttle (it is unauthenticated, off the protected
host, zero anti-bot exposure), and that rule extends here without modification: these two
tools read only bundled package data (`tools/categories.py`'s datasets, `tools/
filters.py`'s `CONDITIONS`) and are registered WITHOUT `require_login` — see
`register_discovery_tools`. Neither tool decides what `search_resumes` accepts; both only
report what `tools/filters.py` and `tools/categories.py` already decided, which is why
every user-facing sentence below is generated from those modules' own exports rather than
retyped (steering/structure.md's Documentation Standards rule — the same rule `tools/
search.py`'s docstrings already follow for the filter-key list and `RESOLVE_ACCEPTS_ZH`).

This module also owns the résumé-row field allow-list (`RESUME_ROW_FIELD_GLOSS`) and the
`p_id` second-identifier note (`SECOND_IDENTIFIER_NOTE`) — moved OUT of `tools/search.py`
(not merely referenced from it) so the glossed allow-list and the prose explaining it move
in one edit; `tools/search.py` imports both back from here rather than the reverse, which
is also what keeps this module free of any dependency on `tools/search.py` itself.
"""

import re

from mcp.server.fastmcp import FastMCP

import mcp104.tools.categories as categories_mod
import mcp104.tools.filters as filters_mod

# ── Mechanical camelCase -> snake_case, moved here from tools/search.py ─────────────
#
# The SINGLE conversion `tools/search.py`'s `_convert_resume_row` applies to every row
# field — moved here (not merely referenced from it) because `describe_result_fields()`
# below must key its payload on the SAME delivered names `_convert_resume_row` actually
# produces — a gloss keyed on 104's raw camelCase names describes fields an Agent holding
# a converted row can never look up (measured once: the intersection was 4 of 35).
# `tools/search.py` imports this back (`from mcp104.tools.discovery import _snake_case`)
# rather than keeping its own copy, so the two can never independently drift on what
# "the delivered name" means for a given raw field. No cycle: this module has no
# dependency on `tools/search.py`, so the import direction stays one-way, same as
# `RESUME_ROW_FIELD_GLOSS`/`SECOND_IDENTIFIER_NOTE` below.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _snake_case(key: str) -> str:
    """Mechanical camelCase -> snake_case: insert '_' before every interior uppercase
    letter, then lowercase. No acronym grouping, no special-casing of digits or runs of
    capitals — "mechanical" is the point: a smarter converter would need updating every
    time 104's own naming drifts, and would get it wrong silently in between.
    """
    return _CAMEL_BOUNDARY_RE.sub("_", key).lower()


def _row_facing_key(camel_key: str) -> str:
    """The key a caller actually finds on a DELIVERED row — `_snake_case` plus the one
    rename `tools/search.py`'s `_convert_resume_row` applies (`idNo` -> `candidate_id`).
    Must never diverge from `_convert_resume_row`'s own logic (same helper, same rename
    condition): a payload describing raw 104 names is not useful to a caller who only
    ever sees the converted row.
    """
    converted = _snake_case(camel_key)
    return "candidate_id" if converted == "id_no" else converted


# ── The résumé-row field allow-list, moved here from tools/search.py ────────────────
#
# 104's field name (camelCase, exactly as it appears on a raw row from search/recommend/
# match) -> a short Traditional Chinese gloss. `in` membership (`k in RESUME_ROW_FIELD_
# GLOSS`) keeps working exactly as it did against the old bare `frozenset` — a dict tests
# membership against its keys — so `tools/search.py`'s row-filtering line (which filters
# the RAW, pre-conversion dict) does not change shape, only its import source.
# `describe_result_fields()` below re-keys this via `_row_facing_key` before publishing
# it, so the allow-list itself stays camelCase (what `search.py` needs to filter on) while
# the published payload is snake_case (what a caller can actually look up).
#
# A gloss states only what is MEASURED. Any field whose meaning has not been measured
# carries 104's own field name verbatim and says plainly that the meaning is unmeasured —
# a plausible-sounding guess here is worse than an admitted gap, because a guess reads as
# fact once it is in a tool's published output. `docs/104-site-facts.md` §6b.2 lists every
# row field BY NAME only, with no meaning recorded for most of them — the boundary below
# is drawn from that same evidence class, not from which fields merely "look"
# self-evident: `remoteWork` asserting "是否可遠端工作" would be a semantic claim
# nothing measured, and `readStatus` is ALSO a search *parameter* (`readStatus=all`), so
# its row meaning cannot be assumed from the name alone either.
RESUME_ROW_FIELD_GLOSS: dict[str, str] = {
    "age": "年齡",
    "areaDesc": "工作地敘述（文字）",
    "contactPrivacy": "聯絡方式是否公開的旗標；'private' 時，get_resume_detail 回傳的 "
                       "email/phone_desc_all/call_time_desc 是佔位文字而非真實聯絡資料——"
                       "這些聯絡欄位本身不在列（row）的回傳範圍內，只有本旗標會出現在列上",
    "documentSnapshotId": "未量測，104 自己的欄位名稱（documentSnapshotId）照登",
    "eduDesc": "最高學歷敘述（文字）",
    "expJobArr": "工作經歷陣列",
    "expPeriodDesc": "工作年資敘述（文字）",
    "hasInRecruitFlow": "未量測，104 自己的欄位名稱（hasInRecruitFlow）照登",
    "hunterStatus": "未量測，104 自己的欄位名稱（hunterStatus）照登",
    "idNo": "候選人 id；本工具轉換後的鍵名是 candidate_id（本欄位不會以 id_no 這個名稱"
            "出現在列上，idNo 是唯一的重新命名例外）",
    "isContact": "未量測，104 自己的欄位名稱（isContact）照登",
    "isHalfShow": "未量測，104 自己的欄位名稱（isHalfShow）照登",
    "isSave": "未量測，104 自己的欄位名稱（isSave）照登",
    "isTalent50": "未量測，104 自己的欄位名稱（isTalent50）照登",
    "latestMemo": "未量測，104 自己的欄位名稱（latestMemo）照登",
    "masterUrl": "履歷主檔連結（僅 search_resumes 帶有，recommend/match 不帶）",
    "nationality": "國籍代碼（僅 search_resumes 帶有，recommend/match 不帶）",
    "otherCourse": "未量測，104 自己的欄位名稱（otherCourse）照登",
    "pId": "第二個候選人識別碼，不是 candidate_id",
    "personalPic": "未量測，104 自己的欄位名稱（personalPic）照登",
    "plastActionDateDesc": "最近活動日敘述（僅 search_resumes 帶有，recommend/match 不帶）",
    "readStatus": "未量測，104 自己的欄位名稱（readStatus）照登——同名也是一個搜尋參數"
                  "（readStatus=all），列上這個欄位的意義未必相同，不可互相推論",
    "remark": "備註（含 HTML）",
    "remarkWithoutHtml": "備註（已去除 HTML）",
    "remoteWork": "未量測，104 自己的欄位名稱（remoteWork）照登",
    "resumeType": "未量測，104 自己的欄位名稱（resumeType）照登",
    "resumeVersion": "未量測，104 自己的欄位名稱（resumeVersion）照登",
    "sex": "性別代碼",
    "sexDesc": "性別敘述（文字）",
    "switchStatusView": "未量測，104 自己的欄位名稱（switchStatusView）照登",
    "titleCatDesc": "應徵職務類別敘述",
    "updateDayDesc": "履歷更新日敘述",
    "userName": "候選人姓名",
    "versionNo": "未量測，104 自己的欄位名稱（versionNo）照登",
    "wcityNoDesc": "居住地敘述",
}

# `p_id` is a THIRD candidate-id key space — not the résumé `candidate_id`, not the
# messaging `candidate_id`. Moved here (from tools/search.py) rather than duplicated, so
# search_resumes' docstring and describe_result_fields' payload read the identical text —
# see this module's own docstring for why the import direction runs tools/search.py ->
# here, never the reverse.
#
# Restated as UNMEASURED IN BOTH DIRECTIONS, not "known distinct" (the JSON-API
# messaging migration): docs/104-site-facts.md §6b.8-2 measured only that the inbox
# row's own `pId` is the id the conversation path takes — it says nothing about
# whether THIS row's `p_id` (search_resumes/list_recommended_resumes/
# list_matched_resumes/get_resume_detail) shares that space, in either direction. The
# operational ban does not weaken for this — it strengthens: "we do not know they are
# the same" is firmer ground for refusing to bridge them than the old "we know they
# differ", which was never actually measured.
SECOND_IDENTIFIER_NOTE = (
    "每筆結果也帶有 p_id（104 的另一個識別碼）。p_id 與 candidate_id 是否同一個 key "
    "space 雙向皆未量測（不是「已知不同」），與 messaging 工具的 candidate_id 是否相同"
    "也未確認 —— 正因為未知，才更不能用 p_id 呼叫任何要求 candidate_id 的工具（見 "
    "CLAUDE.md「候選人 id 不是同一個 key space」）。"
)


# ── Messaging row field allow-lists (read_messages/get_conversation), moved here for ──
# ── the same reason RESUME_ROW_FIELD_GLOSS lives here: tools/messaging.py imports these ──
# ── back, one-way (this module never imports tools/messaging.py) — the constraint that ──
# ── forced EVENT_LABELS to live here too (see below), not in tools/messaging.py as an ──
# ── earlier draft of the migration plan sketched, because describe_result_fields() must ──
# ── generate the event_progress/event_polarity gloss text FROM the same table
# ── _event_labels() consults, and this module cannot read a table living downstream of
# ── it without inverting the dependency direction structure.md requires. ─────────────────
#
# Both allow-lists are the measured field-name sets from
# research/results/messaging_contract.json's own field-name sweep (inbox_id_mapping.
# row_field_names, conversation_semantics.api_message_field_names) — 19 and 13 fields
# respectively, cross-checked against docs/104-site-facts.md §6b.7's table (same counts,
# independently derived) rather than typed from that table's prose, per this project's
# own "a captured/measured artifact outranks a hand-typed transcription of it" rule
# (structure.md's ★★, and D2-V's `metadata`/`fitnessReportStatus` transcription loss).

INBOX_ROW_FIELD_GLOSS: dict[str, str] = {
    "candidateName": "候選人姓名；本工具轉換後的鍵名是 candidate_name",
    "content": "這段對話最後一則訊息的內容",
    "contentFrom": "未量測，104 自己的欄位名稱（contentFrom）照登",
    "eventStatus": "事件狀態代碼；與 eventType 這一對決定 event_progress/event_polarity 的推導，"
                   "語意見 describe_result_fields(row_type=\"inbox\") 回傳的 event_progress 說明",
    "eventType": "事件類型代碼：1=面試邀約、2=主動接觸／詢問；0 代表這段對話目前沒有任何事件，"
                 "不是第三種類型",
    "fitnessReportStatus": "未量測，104 自己的欄位名稱（fitnessReportStatus）照登",
    "hid": "履歷空間形狀的識別碼，key space 未量測，本工具不發布這個欄位——它不會以任何名稱"
           "出現在列上，這裡列出是為了說明它存在且為何被排除",
    "hrHasRead": "不是我方的已讀狀態——30 列樣本中 3 true / 27 false，而同一批的 unread-stream "
                 "回報未讀數是 1，兩者對不起來，且與 hrHasSubscribed 的分布完全相同，較像訂閱旗標"
                 "而非已讀旗標；意義未量測",
    "hrHasSubscribed": "未量測，104 自己的欄位名稱（hrHasSubscribed）照登",
    "id": "未量測，104 自己的欄位名稱（id）照登",
    "idNo": "履歷空間形狀的識別碼（13 位數），key space 未量測，本工具不發布這個欄位——同 hid，"
            "列出是為了說明它存在且為何被排除（見 CLAUDE.md「候選人 id 不是同一個 key space」）",
    "isSemiOn": "未量測，104 自己的欄位名稱（isSemiOn）照登",
    "jobName": "職缺名稱",
    "jobNo": "職缺代碼；本工具轉換後的鍵名是 job_id（保留舊名，且與 list_jobs 回傳的 jobno 已"
             "驗證是同一個 key space，見 CLAUDE.md）",
    "lastMessageId": "未量測，104 自己的欄位名稱（lastMessageId）照登",
    "newestEventTime": "最新事件時間；本工具轉換後的鍵名是 newest_event_time",
    "newestMessageTime": "最新訊息時間；本工具轉換後的鍵名是 newest_message_time",
    "pId": "候選人 id（訊息系統自己的 key space）；本工具轉換後的鍵名是 candidate_id。⚠ 104 "
           "自己的欄位名稱就是 pId——與 search_resumes 等工具列上同樣叫 p_id 的欄位撞名，但雙向"
           "皆未量測是否為同一個 key space，不可互相推導，見 CLAUDE.md",
    "type": "未量測，104 自己的欄位名稱（type）照登",
}

# Excluded from a delivered inbox row entirely — never published under ANY name, row-
# facing or raw. Kept as dict entries above (not simply omitted) so the exclusion is
# documented rather than silent: the gloss payload says the row carries them and why
# they are not surfaced.
INBOX_ROW_EXCLUDED_FIELDS: frozenset[str] = frozenset({"idNo", "hid"})


MESSAGE_ROW_FIELD_GLOSS: dict[str, str] = {
    "content": "訊息內容",
    "createdAt": "訊息建立時間；本工具轉換後的鍵名是 created_at，格式 \"YYYY-MM-DD HH:MM:SS\"",
    "event": "結構化招募動作物件（面試邀約等），絕大多數訊息是 {}；有值時是完整的事件物件，"
             "與 GET /bc-comm/event/* 巢狀回傳的 event 同形（本工具刻意不呼叫那條路由，見 "
             "CLAUDE.md）。⚠ event.content 是我方自己發出的信件本文（可能含員工姓名、公司"
             "名），不是 104 的樣板文字，也不是候選人自己的話",
    "file": "未量測，104 自己的欄位名稱（file）照登",
    "id": "訊息 id；是 read 欄位（讀取狀態）推導的依據之一",
    "idNo": "履歷空間形狀的識別碼，key space 未量測，本工具不發布這個欄位——列出是為了說明它"
            "存在且為何被排除",
    "isSynchronized": "未量測，104 自己的欄位名稱（isSynchronized）照登",
    "ogMeta": "未量測，104 自己的欄位名稱（ogMeta）照登",
    "snapshotId": "未量測，104 自己的欄位名稱（snapshotId）照登，且整段對話同一值，不能當識別"
                  "用途；本工具不發布這個欄位",
    "source": "0=我方送出、1=候選人送出；本工具不直接發布這個欄位，改發布由它推導出的 "
              "direction（sent/received），避免同一件事出現在兩個鍵上",
    "type": "決定這則訊息在網頁版對話串裡是否曾經渲染出來——type: 6 的訊息網頁版看不到，本工具"
            "仍會回傳它；type 本身代表什麼未量測，量到的只有這個渲染效果",
    "unsend": "未量測，104 自己的欄位名稱（unsend）照登",
    "userName": "個資，本工具不發布：對收到的訊息這是候選人姓名（已經在收件匣列上看得到），"
                "對送出的訊息這是操作者本人的姓名——direction 已經回答了是誰發的，這裡不需要"
                "也不應該再洩漏一次姓名",
}

MESSAGE_ROW_EXCLUDED_FIELDS: frozenset[str] = frozenset({"idNo", "snapshotId", "userName", "source"})


# ── Event labels: (event_type, event_status) -> (progress label, polarity), the ──────
# ── derivation behind an inbox row's event_progress/event_polarity ───────────────────
#
# [M docs/104-site-facts.md §6b.10-2, §6b.10-3]. Lives here, not in tools/messaging.py,
# so describe_result_fields(row_type="inbox") can generate its gloss text FROM this
# table rather than retyping it — tools/messaging.py imports both EVENT_LABELS and
# _event_labels from here for its own row conversion, the same one-way direction
# RESUME_ROW_FIELD_GLOSS/_snake_case already establish.
#
# ★ Keyed on the PAIR, not on `status` alone — load-bearing, not tidy. `status == 1`
# reads 面試同意 under an interview (eventType 1) and 有意願 under an inquiry (eventType
# 2): the same code, two different words. A status-keyed table would get today's four
# rows right and start mislabelling SILENTLY the first time a new event type appears —
# and 104 has five more event routes live already (感謝函/面試邀約/詢問/測驗/意願確認,
# §6b.9-3), so that is a matter of when, not if.
EVENT_LABELS: dict[tuple[int, int], tuple[str, str]] = {
    (1, 1): ("面試同意", "positive"),
    (2, 1): ("有意願", "positive"),
    (2, 2): ("無意願", "negative"),
    (2, 3): ("未回覆", "neutral"),
}


def _event_labels(event_type, event_status) -> tuple[str | None, str | None]:
    """Pure. `event_type` falsy (None or 0) means the row has NO event at all — 0 is
    not a third type (§6b.10-2) — so this returns (None, None) rather than inventing a
    neutral label. A `(event_type, event_status)` pair absent from EVENT_LABELS is an
    unlabelled code, not an error: the raw pair is folded into an explicit marker
    string rather than guessed at or dropped, so a row with an unknown status still
    carries its event_type/event_status intact (§6b.10-3: status has at least 7 values
    per-message, 213 conversations sampled from an inbox row that shows only the
    newest event's status)."""
    if not event_type:
        return None, None
    found = EVENT_LABELS.get((event_type, event_status))
    if found is None:
        return f"未知配對（event_type={event_type}, event_status={event_status}），未量測", None
    return found


def _event_label_gloss() -> dict[str, str]:
    """Generates the event_progress/event_polarity gloss text FROM EVENT_LABELS' own
    content — never a hand-typed enumeration of the pairs, so the published text
    cannot silently drift from the table _event_labels() actually consults (the same
    discipline tools/search.py's _filter_key_reference() already applies to
    tools/filters.py's CONDITIONS)."""
    pairs_text = "、".join(
        f"({event_type},{event_status})→{label}/{polarity}"
        for (event_type, event_status), (label, polarity) in sorted(EVENT_LABELS.items())
    )
    return {
        "event_progress": (
            "中文的事件進度標籤，由 (event_type, event_status) 這一對推導，不是 104 渲染的"
            f"文字。已知配對：{pairs_text}。event_type 為 0（這段對話沒有任何事件）時本欄位"
            "為 null；配對不在已知清單內時，回傳帶有原始代碼的「未知配對」說明文字，不會猜"
            "一個看起來接近的標籤，也不會把該列拿掉。"
        ),
        "event_polarity": (
            "事件進度的正負向（positive/negative/neutral），與 event_progress 用同一個 "
            f"(event_type, event_status) 推導，已知配對：{pairs_text}。event_type 為 0 或"
            "配對未知時本欄位為 null。"
        ),
    }


# ── browse_filter_values ─────────────────────────────────────────────────────────────

_COMPLETENESS_NOTES: dict[str, str] = {
    # An earlier revision of this text bolted `sex`/`empStatus`'s own
    # CERTIFICATION METHOD (exhaustive-sum against baseline) onto every "certified"
    # row, plus a claim that every listed code "actually filters" — both false for
    # most of the 14 certified rows today: the two date keys' codes are NESTED
    # WINDOWS, not a sum-to-baseline partition, and their own code `1`（不限）is
    # listed while explicitly NOT filtering (its own label says so); the seven
    # single-flag keys (`auto`/`photo`/`disability`/`driver`/`transport`/`workShift`/
    # `contactPrivacy`) have exactly one code each, nowhere near baseline. A note that
    # needs a per-key exception list to stay true is the wrong note — `domain_
    # completeness` answers ONE question ("is the code set complete"), and that is
    # the only claim this string now makes. Per-code specifics (which code is a
    # no-op, which certification method applied) already live in that row's own
    # `value_domain` label, where each claim is true of the code it describes.
    "certified": "values 是這個鍵目前已知的完整代碼集合（不會再有值域外的合法代碼）；"
                 "個別代碼的意義與是否會實際套用篩選，見各自的標籤文字。",
    "measured-subset": "values 只列出已量測確認生效的代碼，不代表值域已窮盡 —— 未列出"
                        "的代碼可能存在但未經測試，不要假設 values 之外沒有其他合法值。",
    "unmeasured": "此鍵的值域完全未量測；values（如果有值）僅供參考，正確性未經驗證。",
}


def _completeness_note(domain_completeness: str) -> str:
    """The one place `Condition.domain_completeness` turns into caller-facing prose —
    three DIFFERENT strings for the three possible values, so the distinction this field
    exists to carry actually reaches whoever calls `browse_filter_values`, not just the
    condition table.
    """
    return _COMPLETENESS_NOTES[domain_completeness]


def _node_dict(node: categories_mod.BrowseNode) -> dict:
    return {
        "code": node.code, "name": node.name,
        "terminal": node.terminal, "has_children": node.has_children,
    }


def _browse_dataset_key(condition: filters_mod.Condition, code: str | None) -> dict:
    """`value_source == "dataset"`: children of `code`, or the root layer when `code is
    None`. Never calls `categories_mod._is_terminal` directly — `categories_mod.children`/
    `categories_mod.path` already return nodes with `terminal` computed, keyed on the
    (file, condition) pair via the already-loaded `Dataset`.
    """
    dataset = categories_mod.load_dataset(condition.dataset, condition.key)
    try:
        values = categories_mod.children(dataset, code)
    except KeyError:
        return {
            "error": f"篩選鍵 '{condition.key}'（資料集 {condition.dataset}）沒有代碼 "
                     f"'{code}'"
        }
    path_nodes = categories_mod.path(dataset, code) if code is not None else ()
    is_leaf_choice = code is not None and not values
    note = (
        "此代碼是葉節點，可直接選用；此節點沒有下一層可再瀏覽。" if is_leaf_choice
        else "104 的分類樹一層；每個節點是否可直接選用見 terminal，是否還有下一層可瀏覽"
             "見 has_children。"
    )
    return {
        "key": condition.key,
        "value_source": "dataset",
        "dataset": condition.dataset,
        "path": [_node_dict(n) for n in path_nodes],
        "values": [_node_dict(n) for n in values],
        "note": note,
    }


def _browse_enum_key(condition: filters_mod.Condition) -> dict:
    return {
        "key": condition.key,
        "value_source": "enum",
        "values": dict(condition.value_domain),
        "note": _completeness_note(condition.domain_completeness),
    }


def _browse_composite_key(condition: filters_mod.Condition) -> dict:
    """`structure` is `condition.caller_shape` — a REAL caller-facing dict shape
    (field names, required-ness, nesting), never `condition.value_domain`:
    `value_domain` for a composite row is prose CAVEATS about the shape
    (unmeasured sub-domains, dotted-key annotations like `"month.mode"`), not a shape a
    caller could build a request from — using it here produced an invalid `filters`
    dict on every single composite call. `caller_shape` is derived directly from each
    composite's own `_validate_*` function in this module's sibling `filters.py` (see
    that field's own comment on `Condition`), never hand-typed a third time here.
    """
    # `sub_domains` is `value_domain` — the same prose the paragraph above refuses to
    # use as `structure`, surfaced under its own key because the two answer different
    # questions. Simply dropping `value_domain` once it stopped masquerading as a shape
    # would silently cost the caller the only record of
    # which sub-fields are still unmeasured. `language_skills` shows the damage: its
    # structure says `'language': <代碼>` and, without this key, nothing anywhere in
    # the tool's output tells a caller that only 1=英文 and 2=日文 are known or that
    # the domain was never obtained. An Agent was being asked for a code it had no path
    # to discover — the exact failure `browse_filter_values` exists to prevent.
    return {
        "key": condition.key,
        "value_source": "composite",
        "structure": condition.caller_shape,
        "sub_domains": dict(condition.value_domain or {}),
        "domain_completeness": condition.domain_completeness,
        "note": "結構化條件，structure 是可直接依樣建構的 dict shape；建好後傳入 "
                "search_resumes(filters={\"" + condition.key + "\": ...}) 對應鍵下。"
                "⚠ sub_domains 是各子欄位的值域註記，與 structure 是兩回事：structure "
                "說形狀，sub_domains 說每個子欄位收什麼值、以及該值域是否已量測完整"
                "（標示 [INF] 者代表尚未取得完整值域，不要假設沒列出的值不合法）。",
    }


def _browse_codes_only_key(condition: filters_mod.Condition) -> dict:
    # No `CONDITIONS` row uses `value_source == "codes-only"` today (full sweep: enum 16,
    # dataset 11, numeric 3, composite 3, free-text 2 = 35 rows) — kept reachable, not
    # speculative, so the day a row IS added here it is already covered rather than
    # silently falling through to the free-text/numeric branch below.
    return {
        "key": condition.key,
        "value_source": "codes-only",
        "note": "此鍵只接受 104 自己的分類代碼，沒有內建資料集可供瀏覽；請自行取得代碼再"
                "傳入。",
    }


def _browse_scalar_key(condition: filters_mod.Condition) -> dict:
    kind = "數字" if condition.value_source == "numeric" else "自由文字"
    note = f"此鍵是{kind}輸入，沒有值域可瀏覽"
    if condition.value_domain:
        note += f"（{condition.value_domain}）"
    note += "。"
    return {"key": condition.key, "value_source": condition.value_source, "note": note}


def _browse_filter_values(key: str, code: str | None) -> dict:
    """Pure decision logic behind the `browse_filter_values` tool — module-level per
    steering/structure.md ("決策邏輯必須抽成模組層級的函式"), directly testable without
    an MCP Context.

    Unknown-key check consumes `filters_mod.VALID_FILTER_KEYS` — the same tuple `tools/
    filters.py`'s own `validate_filters` and `tools/search.py`'s `_filter_key_reference`
    gate on — rather than re-deriving `provenance == "filter-key" and shippable` a third
    time (that predicate is `VALID_FILTER_KEYS`'s own definition; re-deriving it here
    would let this tool silently disagree with `search_resumes` the day a row is retired).
    """
    if key not in filters_mod.VALID_FILTER_KEYS:
        return {
            "error": f"未知的篩選鍵：{key}。合法的篩選鍵："
                     f"{'、'.join(filters_mod.VALID_FILTER_KEYS)}"
        }
    condition = filters_mod.CONDITIONS[key]
    if condition.value_source == "dataset":
        payload = _browse_dataset_key(condition, code)
    elif condition.value_source == "enum":
        payload = _browse_enum_key(condition)
    elif condition.value_source == "composite":
        payload = _browse_composite_key(condition)
    elif condition.value_source == "codes-only":
        payload = _browse_codes_only_key(condition)
    else:
        payload = _browse_scalar_key(condition)  # "numeric" | "free-text"
    return _with_combination_facts(condition, payload)


def _with_combination_facts(condition: filters_mod.Condition, payload: dict) -> dict:
    """Attach how this key COMBINES — with itself and with the rest of `filters`.

    Knowing a key's legal values is not enough to build a query. A caller also has to
    know whether it may ask for several of them at once, and what happens when it sets
    two different keys. Neither was discoverable anywhere: `search_resumes`' description
    is at 1,928 of its 2,048-character budget and cannot take the explanation, and this
    tool answered only "what values", never "how many, and with what meaning".

    That gap had teeth. `edu=['8','16']` is legal and means 大學 or 碩士, so generalising
    to `sex=['0','1']` is correct reasoning from the only rule on display — and until
    `MultiValueNotAcceptedError` existed it put `str(['0','1'])` on the wire, where 104
    may discard it silently and leave the caller believing a filter applied.

    `accepts_multiple` is read off `MULTI_VALUE_KEYS`, the same tuple the validator now
    rejects against, so the advice here and the enforcement there cannot disagree.
    """
    if "error" in payload:
        return payload
    multi = condition.key in filters_mod.MULTI_VALUE_KEYS
    payload["accepts_multiple"] = multi
    # A third axis, and the one most easily mistaken for absent. `driver` matches
    # hierarchically, so `driver=1` returns every motorcycle-licence holder — a caller
    # reading only the label 輕型機車駕照 would believe the result set is narrower than
    # it is, and「只要輕型機車」is a request this filter cannot express at all. Carried
    # only when a row has something measured to say, so silence here never implies a
    # claim was made.
    if condition.matching_note:
        payload["matching"] = condition.matching_note
    payload["combination"] = (
        ("這個鍵可以傳一個 list，表示「其中之一即可」（OR）。"
         if multi else
         "這個鍵只接受單一值；傳 list 會在送出前被拒絕（不會靜默送出）。"
         "要表達多選，只有 accepts_multiple 為 true 的鍵做得到。")
        + "不同的篩選鍵之間一律是 AND（同時滿足），所以加鍵只會讓結果變少、不會變多。"
        + f"目前支援多值的鍵：{'、'.join(filters_mod.MULTI_VALUE_KEYS)}。"
    )
    return payload


# ── describe_result_fields ───────────────────────────────────────────────────────────

def _describe_resume_fields() -> dict:
    """Swept from `RESUME_ROW_FIELD_GLOSS` — never a hard-coded field count, so the
    payload cannot silently fall out of step with the allow-list it describes.

    Keyed on `_row_facing_key(raw_key)`, NOT the raw camelCase key `RESUME_ROW_FIELD_
    GLOSS` itself uses: the allow-list is keyed camelCase because
    `tools/search.py` filters the RAW pre-conversion dict against it, but a caller of
    THIS tool is holding an already-converted row and can only look fields up by the
    name actually printed on it. `_row_facing_key` is the exact same transform
    `_convert_resume_row` applies (imported from the same place `tools/search.py`
    imports it), so this can never silently diverge from what a row really carries.
    """
    return {"fields": {_row_facing_key(k): v for k, v in RESUME_ROW_FIELD_GLOSS.items()}}


def _inbox_row_facing_key(camel_key: str) -> str:
    """The same transform `tools/messaging.py`'s `_convert_inbox_row` applies: mechanical
    `_snake_case`, plus the two documented renames (`jobNo` -> `job_id`, `pId` ->
    `candidate_id`). Excluded fields (`INBOX_ROW_EXCLUDED_FIELDS`) are handled by the
    caller, not here — they have no row-facing name at all, since they never appear on
    a delivered row under any spelling."""
    converted = _snake_case(camel_key)
    if converted == "job_no":
        return "job_id"
    if converted == "p_id":
        return "candidate_id"
    return converted


def _describe_inbox_fields() -> dict:
    """Excluded fields (`idNo`/`hid`) are published under their OWN raw 104 field name
    — never a row-facing name, since they never appear on a delivered row under any
    spelling — so the gloss states the row carries them and why they are excluded,
    rather than silently omitting them. `event_progress`/
    `event_polarity` are appended from `_event_label_gloss()`, generated from
    `EVENT_LABELS` rather than retyped."""
    fields = {}
    for raw_key, gloss in INBOX_ROW_FIELD_GLOSS.items():
        key = raw_key if raw_key in INBOX_ROW_EXCLUDED_FIELDS else _inbox_row_facing_key(raw_key)
        fields[key] = gloss
    fields.update(_event_label_gloss())
    return {"fields": fields}


_READ_STATE_GLOSS = (
    "三態，不是布林值。已讀 ⇔ int(id) <= int(metadata.creadAt)，只對 direction=\"sent\"（我方"
    "送出）的訊息有意義；收到的訊息一律是 null（讀取狀態對「我方收到的訊息」沒有意義，硬填 "
    "false 會誤讀為「對方沒讀自己發的訊息」）。⚠ 兩個必須一起帶著的界限：creadAt 是整段對話"
    "唯一一條水位線，不是逐則資料——「已讀」代表「候選人已讀到這一則為止」；id 與時間單調一致，"
    "但同一秒內可能出現 1–2 個名次差，水位線落在同秒邊界上時可能誤判一則。metadata 缺失或 "
    "creadAt 無法解析時同樣是 null（代表不明，不是「確定未讀」）。"
)

_DIRECTION_GLOSS = (
    "sent=我方送出、received=候選人送出，由 104 的 source 欄位（0/1）推導；source 本身不會"
    "出現在列上（同一件事只留一份）。"
)


def _describe_message_fields() -> dict:
    """Excluded fields (`idNo`/`snapshotId`/`userName`/`source`) are published under
    their own raw 104 field name, same reasoning as `_describe_inbox_fields`.
    `direction`/`read` are appended — the two derived fields `_convert_message_row`
    adds beyond the raw allow-list."""
    fields = {}
    for raw_key, gloss in MESSAGE_ROW_FIELD_GLOSS.items():
        key = raw_key if raw_key in MESSAGE_ROW_EXCLUDED_FIELDS else _snake_case(raw_key)
        fields[key] = gloss
    fields["direction"] = _DIRECTION_GLOSS
    fields["read"] = _READ_STATE_GLOSS
    return {"fields": fields}


_ROW_TYPE_DESCRIBERS = {
    "resume": _describe_resume_fields,
    "inbox": _describe_inbox_fields,
    "message": _describe_message_fields,
}


def _describe_result_fields(row_type: str = "resume") -> dict:
    """Dispatches on `row_type` — `"resume"` (default, preserves today's behaviour
    byte for byte), `"inbox"` (read_messages' rows) or `"message"`
    (get_conversation's rows). An unknown value is an error naming the three legal
    ones, never a silent fallback to `"resume"`."""
    describer = _ROW_TYPE_DESCRIBERS.get(row_type)
    if describer is None:
        return {
            "error": f"未知的 row_type：{row_type!r}。合法值："
                     f"{'、'.join(sorted(_ROW_TYPE_DESCRIBERS))}"
        }
    return describer()


# ── Docstrings — generated, per steering/structure.md's rule that a sentence stating ──
# ── what a tool accepts/returns must come from the definition deciding it. ────────────

def _browse_filter_values_description() -> str:
    return f"""瀏覽 search_resumes 某個 filters 鍵接受哪些值（走本機套件資料，不呼叫 104、
不佔用履歷瀏覽配額）。

Args:
    key: search_resumes 的 filters 合法鍵之一（清單見 search_resumes 自己的說明）。
    code: 選填，僅對資料集背後的鍵（value_source="dataset"）有意義——傳入某分支代碼可
        往下瀏覽一層；不傳則回傳最上層。

回傳固定含 key、value_source、note；依 value_source 另外帶：
  - dataset：dataset（檔名）、path（root 到 code 含本身，根層 []）、values（此層節點，
    各含 code/name/terminal/has_children）。{categories_mod.CANDIDATE_TERMINAL_ZH}。
    terminal 與 has_children 是兩件獨立的事——terminal 為 true 的節點仍可能 has_
    children 為 true。resolve() 接受規則：{categories_mod.RESOLVE_ACCEPTS_ZH}
  - enum：values（代碼→標籤字典）。⚠ note 會指出 values 是否窮盡：某些鍵只列出已
    量測生效的代碼，不是完整值域。
  - composite：structure（可直接依樣建構的巢狀 dict shape，建好後傳入 search_resumes
    對應 filters 鍵）。
  - codes-only / numeric / free-text：只有 note，沒有 values 可瀏覽。

失敗時（未知 key、不存在的 code）回傳 {{"error": str}}，沒有 values/structure。

未帶 code、或帶葉節點的 code：都是成功——葉節點的 values 是 []，note 會說明可直接
選用，不是「查無結果」。

⚠ {SECOND_IDENTIFIER_NOTE}

不需要 login()：讀套件內建公開分類資料，不經過 guarded_api，不消耗
104 請求或履歷瀏覽配額。
"""

def _describe_result_fields_description() -> str:
    return f"""列出「列」（row）回傳的每個欄位是什麼意思（走本機套件資料，不呼叫 104、
不佔用履歷瀏覽配額）。

Args:
    row_type: "resume"（預設，search_resumes / list_recommended_resumes /
        list_matched_resumes 的列）｜"inbox"（read_messages 的列）｜"message"
        （get_conversation 的列）。未帶時等同 "resume"，與這個參數新增前的行為
        逐位元相同。不合法的值回傳 {{"error": str}}，附上三個合法值。

回傳固定為 {{"fields": {{列上實際的欄位名: 繁體中文說明}}}}——鍵是列（row）轉換後
真正看到的名稱（snake_case；resume 的 idNo 顯示為 candidate_id，inbox 的 jobNo/pId
顯示為 job_id/candidate_id），不是 104 未轉換的原始名稱，三種 row_type 都是從各自
的欄位允許清單依同一套轉換規則產生（單一定義來源，不是另一份手寫清單）。含義尚未
量測的欄位會照 104 自己的欄位名稱標註「未量測」，不猜測意義。

⚠ inbox/message 兩種 row_type 額外會有「本工具不發布這個欄位」的條目：這些欄位（如
idNo、hid、snapshotId、source）的 key space 未量測或本身是個資，delivered 的列上
完全不會出現它們（不論原名或轉換後的名稱）——列在這裡是為了誠實說明「列曾經帶有它，
但被拿掉了、以及為什麼」，不是可以查到的欄位名。inbox 的 event_progress/
event_polarity 會列出目前已知的 (event_type, event_status) 配對與對應標籤。

⚠ {SECOND_IDENTIFIER_NOTE}

⚠ contact_privacy == "private" 時（僅 resume）：本旗標會出現在列上，但實際的聯絡欄位
（email / phone_desc_all / call_time_desc）不在列的回傳範圍內，只在 get_resume_detail
才有，屆時是 104 填入的人類可讀佔位文字，不是真實聯絡資料；唯一可靠的依據是 contact_
privacy 本身。

不需要 login()：本工具是純本機說明文字，不經過 guarded_api，不消耗任何
104 請求或履歷瀏覽配額。get_resume_detail 的完整履歷欄位不在本工具範圍內（那些欄位
機械式轉換、不省略，見 get_resume_detail 自己的說明）。
"""


_BROWSE_FILTER_VALUES_DESCRIPTION = _browse_filter_values_description()


def register_discovery_tools(mcp: FastMCP):
    """Registers WITHOUT `require_login` — see this module's own docstring and
    steering/structure.md: public category data must not be gated the way a real 104
    request is, and requiring login here would make the one free tool in the set look
    expensive for no measured security reason (the data is public on 104's own CDN and
    this server is reached only through the operator's own MCP configuration).
    """

    async def browse_filter_values(key: str, code: str | None = None) -> dict:
        return _browse_filter_values(key, code)

    browse_filter_values.__doc__ = _BROWSE_FILTER_VALUES_DESCRIPTION
    mcp.tool()(browse_filter_values)

    async def describe_result_fields(row_type: str = "resume") -> dict:
        return _describe_result_fields(row_type)

    describe_result_fields.__doc__ = _describe_result_fields_description()
    mcp.tool()(describe_result_fields)

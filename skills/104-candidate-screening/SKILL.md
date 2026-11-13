---
name: 104-candidate-screening
description: 用 mcp104（104人力銀行 MCP server）替職缺找人、篩人，把候選人整理成一份可離線閱讀的 HTML 履歷報表（含照片、履歷附件、完整履歷原文），並在使用者拍板後聯絡人選：先查誰已聯繫過、先從帳號的罐頭範本找合適文字、逐位讓真人看過全文再送出。Use this for ANY step of working with 104 candidates, not only the first one — 看推薦履歷或配對人選、幫某個職缺找人、依條件篩履歷、抓大頭照與履歷附件、比較人選的經歷與期望薪資、做成 HTML 報表給主管看、查某人聯絡過沒有、挑或存罐頭訊息範本、送詢問意願／面試邀約／感謝函。It applies when the request is small（「看一下今天的推薦履歷」）, and also mid-workflow, when the user already has a shortlist and never repeats the word「104」because the candidates obviously came from there — 例如「把那四十個人做成一個 html，完整履歷都要在裡面」或「這幾個人選幫我比一下經歷跟期望薪資」。職缺管理（更新、關閉、開啟、刪除、修改、複製新增）只給入口與確認規則。Not for editing mcp104's own code or config. Carries the quota-safe call order (only get_resume_detail costs the 300/day budget), the résumé record schema, the screening-group method, the template-first contact step with human sign-off on every send, and a bundled renderer.
compatibility: 需要已設定並登入的 mcp104 MCP server；產生報表需要 Python 3 與 Pillow。
---

# 104 人選篩選與履歷報表

## 這個 skill 在解決什麼

104 的招募後台一次給你幾百筆履歷，但真正稀缺的東西只有一個：**每日 300 筆的履歷詳情配額**。
清單看不到期望待遇、看不到完整工作內容、看不到自傳，只有履歷詳情看得到——而履歷詳情是唯一
會扣配額的呼叫。

所以整個流程的形狀是被這件事決定的：**先用免費的清單欄位把範圍縮小，再把配額花在值得花的
人身上。** 底下的順序不是流程圖，是成本結構。

另一個真實限制：使用者在跟你對話的當下**通常沒辦法自己去開 104 網頁**（要登入、而且 104 是
單一登入，他一登入就把 MCP 踢掉了）。所以「給連結讓他自己看」等於沒有交付。履歷內容必須落
成他打得開的檔案。

## 流程

### 1. 先確認要找的是哪個職缺

`list_jobs()` 拿到 jobno。使用者說「應用工程師」的時候，公司裡可能同時開著客服維修工程師、
設備工程師好幾個相近職缺，人選池高度重疊——**問清楚是哪一個 jobno，不要猜**，因為後面所有
清單呼叫和發訊息都綁在 jobno 上。

### 2. 取清單（不花配額，放心多抓）

先搞懂 104 給的幾種名單是怎麼來的——它們不是同一份資料的不同排序，是**不同的產生
機制**，決定了什麼時候該用哪一個：

| 名單 | 從哪裡來 | 工具 |
|---|---|---|
| **推薦** | 104 拿**你貼的職缺內容**去比對履歷庫，自動產生。職缺設好就有，你不用做任何事 | `list_recommended_resumes(jobno)` |
| **配對** | 104 拿**你在後台存好的搜尋條件**去比對，而且**每天更新**。條件是你自己設的，所以通常比推薦準 | `list_matched_resumes(jobno)` |
| **搜尋** | 你當下丟關鍵字即時查，跟職缺設定無關 | `search_resumes(...)` |
| **應徵** | 求職者自己投遞這個職缺（近 60 天），不是 104 猜的，所以通常最先看 | `list_applicants(jobno)` |

實務上的取捨：**推薦量大但雜**（演算法猜的），**配對量少但準**（你自己定義的條件）。
要廣就從推薦撈，要快就先看配對。

**配對可以只看今天新增的**：`list_matched_resumes(jobno, update_date_type="2")` 就是
104 網頁上那個「當日配對」分頁。這是每天回頭看有沒有新人選最省的方式——不必重篩整池。
沒帶這個參數就是全部。

⚠ 當日配對零筆時，104 回的是 `resumes: []` 加上 `pageInfo: null`（鍵在、值是 null），
不是錯誤。這個形狀會讓假設 `pageInfo` 一定存在的程式炸掉，看到它就是「今天沒有新的」。

⚠ **關閉中的職缺，兩條路由行為不一樣**：`switch="off"` 的職缺在**配對**路由回 404、
在**推薦**路由回成功加空陣列。所以某個職缺配對查不到時，先確認它是不是已經關了，
不要當成「今天沒人」。職缺本身要不要重新開，見底下「職缺本身出問題時」，那是要使用者拍板的事。

這四條都**不扣履歷瀏覽配額**（repo 的 `docs/104-site-facts.md` §6b.4 有單一變因的對照
實驗，而且推翻過一次相反的結論，值得一讀）。要全部就翻完所有頁，不要只抓第一頁然後說
「今天的」——使用者說「看推薦履歷」多半是指整池，不是指今天新增的那幾筆。不確定就問。

推薦與配對的**列欄位完全相同**（32 個，逐一比對無差異），搜尋是這 32 個再加兩個
（`nationality`、`plast_action_date_desc`）。所以同一套篩選程式可以直接套在三種名單上。

⚠ **不要期待列上有 `master_url`。** 104 的搜尋路由確實會送這個欄位（履歷頁的網址），但
mcp104 從 0.2.2 起**刻意不轉出來**：那個連結要用 MCP 這個行程手上的登入才開得了，Agent
面前的真人點不開，而且每一列還要多帶一份 URL 編碼的整個查詢。要把履歷呈現給人看就讀
`get_resume_detail`，要檔案就用 `get_candidate_photo`／`get_resume_attachment`。

把清單存成一個 JSON 檔（`cands.json`）再開始篩，不要留在 context 裡。幾百筆清單塞進對話
會把後面讀履歷的空間吃光。

### 3. 粗篩：只用清單欄位

清單有的欄位大致是：姓名、年齡、性別、地區、希望工作地、學歷、`title_cat`（希望職稱類別）、
年資、更新日期，以及每段工作的公司名稱。**期望待遇不在清單裡**，所以這一步篩不了薪資，別
試——先用經歷和職稱類別把人數壓到你願意花配額的規模。

怎麼把使用者的條件變成可重現的篩選規則，見 `references/screening-method.md`。核心是：把
「我想要 A 或 B、不要 C 或 D」拆成**幾個具名的組**加上**幾條硬排除**，然後把每個人歸到組
裡並寫一句理由。組別後面會變成報表的分區與篩選鈕，理由會變成卡片上的評註——所以這一步做
得夠具體，報表就自己長出來了。

### 4. 讀履歷詳情：唯一花配額的一步

`get_resume_detail(candidate_id)`，一位一格，扣的是當天的 300。

**同一位候選人當天重複讀不會重複扣，但隔天再讀會再扣一格**（2026-09-05 實測，數據見
`references/mcp104-notes.md` §2）——它是「每人每日」不是「每人永久」。這條規則直接
決定下一步該什麼時候做。

讀回來的東西要**轉寫成結構化 JSON 再存檔**，格式見 `references/record-schema.md`。實務上
一次寫 5～6 筆一個檔（`b1.json`、`b2.json`…）最後合併，原因有兩個：一次塞 40 筆進一個工具
呼叫會爆，而且中途失敗時你不會賠掉全部。**用 Write 工具寫這些檔，不要用 shell heredoc**
——大段中文加上反斜線在 heredoc 裡會被吃掉，這個坑很花時間。

期望待遇（`hope_salary_desc`）只在履歷詳情裡。薪資篩選一定是在這一步之後做，不是之前。

### 5. 照片與附件：跟上一步同一天做

`get_candidate_photo(candidate_id)` 和 `get_resume_attachment(candidate_id, sort)` 各自會
先讀一次履歷詳情才去拿檔案。**當天已經讀過詳情的人，這裡是免費的；隔天才補抓，每個人要多
付一格配額**——配額的 key 是（候選人，日期），不是候選人。

所以不要把「先篩完再說，照片明天再抓」當成省事，那是加倍付錢：40 個人隔天補抓就是 40 格，
一天配額的 13%。**這一步要跟上一步在同一天收尾**，收不完就接受重新計費，不要以為名單在手
上就不用再付。

查一份履歷有哪些附件，用一個不存在的 `sort`（例如 999）呼叫 `get_resume_attachment`，錯誤
回應會附上 `available` 清單。這比為了看 `attach_arr` 而重讀整份履歷詳情省下大量 context。

**抓下來的檔案 24 小時後會自動清除，`logout()` 會立刻清除。**所以拿到之後第一件事是複製到
輸出資料夾，不要放著等最後再說。

沒放照片的人，104 回的不是 404 也不是空的，是一張固定的灰色人形剪影 PNG。**0.3.1 起
`get_candidate_photo` 自己就會認出它、回 `photo: null`**（0.3.0 以前會把它當成正常照片
回傳）；`build_report.py` 另外還有一道 md5 比對，擋的是不經工具拿到的檔案，顯示成「無照片」
而不是一張糊掉的圖。兩道規則各自寫死一個摘要值，見 `references/mcp104-notes.md` §3。

### 6. 產生報表

```bash
python scripts/build_report.py --config report_config.json
```

設定檔怎麼寫見 `references/record-schema.md` 的「report_config.json」一節；跑 `--init` 可以
產生一份範本。

報表是**單一個自足的 HTML 檔**：照片內嵌成 base64，沒有外部連結、沒有 CDN、離線可讀、可以
直接寄給人。版面是「摘要恆常可見 + 三個可展開區段」，因為使用者要的是先掃過 40 個人再決定
展開誰，而不是被 40 份完整履歷淹掉。

做完務必自己開一次確認版面沒壞（headless 瀏覽器截圖即可）。這個腳本是用字串替換把設定塞進
模板的，語法錯誤不會讓它失敗，只會讓頁面靜靜地渲染不出來。

### 7. 提名要聯絡的人之前，先查誰已經聯絡過

`check_already_contacted(candidate_id, id_source)` 回 True／False；要看是哪一筆、什麼狀態、
何時記下，用 `get_candidate_status`；`list_tracked_candidates(status=…)` 列出本機記下狀態的
人。這三個查的是**本機 SQLite**：零個 104 請求、不扣配額、不佔節流名額，所以對整份短名單逐位
查沒有理由省。

`id_source` 看 id 從哪來：履歷列的 `candidate_id`（idNo）填 `"resume"`，訊息系統的 `p_id` 填
`"message"`。兩個 id 拿哪一個查都行：104 把兩個 id 放在同一筆資料上交出時（履歷列、應徵者列、
履歷詳情），本機會記下這組對應，所以拿 `candidate_id` 查，也找得到當初用 `p_id` 寫下的狀態。

⚠ 「沒有紀錄」只代表**這個 Agent 沒記過**。本機只記經由這些工具送出、或你用
`update_candidate_status` 寫下的狀態，同事在 104 後台直接發的訊息不在裡面；一組對應還沒被觀察到
（還沒讀過任何同時帶兩個 id 的資料）時，另一個 id 底下的紀錄也找不到。要確認 104 那一側有沒有
對話，用 `read_messages(job_nos=[jobno])`。

狀態值是 `contacted`、`replied`、`interested`、`not_interested`、`scheduled`。送出工具會自己
寫（詢問意願等寫 `contacted`、邀約面試寫 `scheduled`），`replied` 之類要靠
`update_candidate_status` 記——記使用者告訴你的結果，不要自己猜。

### 8. 聯絡人選：範本優先，逐位由真人確認

誰要聯絡、用什麼措辭，是使用者的決定。你負責備好選項，他說發哪幾位，然後**逐位**讓他看過
完整文字再送：一次呼叫恰好一位，沒有批次。每一次送出都**立刻到真人手上、收不回來、沒有
dry-run**。

先選工具。事件種類由工具決定，不是由範本決定：

| 要做的事 | 工具 | 範本 `type_id` |
|---|---|---|
| 問有沒有意願 | `send_inquiry` | `"1"` |
| 約面試（另需 `meeting_time`、`location`、`phase`、`contact_name`、`contact_phone`） | `send_interview_invite` | `"2"` |
| 感謝函（通常用於婉拒） | `send_thank_you_letter` | `"3"` |
| 通知報到（另需 `start_time`、`location`、聯絡人） | `send_start_date_reminder` | `"4"` |
| 邀性格測驗 | `send_personality_test_invite` | `"5"` |
| 一般文字訊息（沒有 `template_id` 參數，範本文字要自己貼進 `message`） | `send_message` | `"0"` 不分類，或任何一類 |

**寫之前先看帳號裡有沒有現成的範本。** 呼叫 `list_templates(type_id=…)`（用上表的對應，沒有合適
分類就省略 `type_id` 看全部）。每筆的 `description` 就是完整信件本文，不必另外查。這個呼叫不扣
履歷配額，但是一個真的 104 請求、佔一個節流名額。挑得到合適的就拿它當底稿，不要重寫；
都不合適才從頭寫。

⚠ **`#jname#` 要自己換。** 範本本文裡可能有 `#jname#`，代表職缺名稱。送出工具把文字原樣
送出，104 會不會在伺服器端替換它**沒有量測過**。所以由你把 `#jname#` 換成真正的職缺名稱
（`list_jobs` 那一列的 `job`），**換好的文字**拿給真人看，也用換好的文字送。賭 104 會替你換，
賭輸的後果是候選人收到一封裡面寫著 `#jname#` 的信。

送出時：

- `message` 放最後確認過的全文（≤ 1000 字元），`template_id` 放範本的 id（選填，挑同類別的）。
  ⚠ 送出的永遠是 `message`，**不是**範本的 description——工具不會把範本當成預設內容，所以真人
  看到的和實際送出的必須是同一段文字。
- `job_id` 用 `list_jobs` 的 `jobno`；`candidate_id` 用履歷列或應徵者列的 **`p_id`**，不是
  idNo（位數 ≥ 12 的會在送出前被擋下）。
- 時間用 `YYYY-MM-DD HH:mm`（台北時間）。邀約面試與到職提醒**再呼叫一次是送出第二封**，不是
  改期也不是取消前一封；改期請到 104 後台。
- 回傳的 `sent` 只有 `"confirmed"` 與 `"unconfirmed"`，照值判斷。`unconfirmed` 很可能已經
  送達，**不要重送**。
- **逾時也不要重送。** 用戶端逾時時信可能已經到了；先用 `read_messages(job_nos=[jobno])` 或
  104 後台確認。
- 成本：每封事件信佔 3 個節流名額（每小時上限 300 個請求）；每日發送上限預設 50 則，五個
  事件工具與 `send_message` 共用。

**使用者想把這次的措辭留給下次用**，用 `create_template(title, description, type_id)`（標題
≤ 16 字、內容 ≤ 1000 字、`type_id` 必填；新範本沒有附件），改現有的用
`update_template`，不要的用 `delete_template`。這三個**不會送任何東西給候選人**，但都是對帳號的
寫入，要先讓真人確認：`delete_template` 無法復原，而且被職缺使用的範本 104 不讓刪；
`update_template` 遇到帶附件的範本會在送出前拒絕，那種請使用者到 104 後台改。回傳的
`created`／`updated`／`deleted` 若是 `"unconfirmed"`，不代表失敗，去後台確認，不要重送
（重送 `create_template` 會產生重複的範本）。

## 職缺本身出問題時

篩人的時候會撞見職缺本身的問題：職缺已關閉（`switch="off"`，步驟 2 的 ⚠）、刊登內容
和使用者要找的人對不上、想更新職缺日期。這時你可以指路，但**由使用者決定做不做**：

| 工具 | 做什麼 |
|---|---|
| `get_job_detail(jobno)` | 唯讀，讀刊登內容（說明、薪資、地點、條件） |
| `list_refreshable_jobs()` → `refresh_jobs(jobnos)` | 更新職缺日期；只接受前一個工具列出的職缺，沒有「全部更新」 |
| `close_jobs(jobnos, reasons)`／`open_jobs(jobnos)` | 關閉（`reasons` 必填）／重新開啟。開啟會用掉刊登額度，並取消加值廣告預約 |
| `delete_job(jobno)` | 只限**已關閉**的職缺，一次一個 |
| `update_job(jobno, changes)`／`create_job(source_jobno)` | 改刊登內容／以既有職缺為底稿複製新增；先不帶 `confirm` 取得預覽，真人看過差異再帶 `confirm=True` |

共通的規則：**這些都立刻生效、求職者看得到、無法撤回，呼叫前必須先讓真人確認**（要動哪些
職缺、改成什麼）。回傳的 `refreshed`／`closed`／`opened`／`deleted`／`saved`／`created` 都是靠
寫入後重讀來判斷，`"unconfirmed"` 不代表失敗，用 `list_jobs` 或 104 後台確認，不要重送。

⚠ `delete_job` 會連這個職缺的**應徵履歷與聯絡紀錄**一起刪（依 104 介面的確認文字）。你為這個
職缺篩過的應徵者就在裡面——刪之前先確認報表與檔案都已經交給使用者。

⚠ `create_job` 逾時後**絕對不要重送**：儲存可能已經送出，重送會多出第二個公開職缺。先用
`list_jobs` 看新職缺有沒有出現。新職缺開不開放以 104 回報的 `new_job_switch` 為準。

## 候選人資料怎麼處理

這些是真人的姓名、電話、Email、照片和完整工作史。

- 報表和附件**只寫本機檔案**。不要發布成 Artifact、不要上傳到任何外部服務、不要放進會被
  索引的地方。使用者要不要分享是他的決定，不是你的。
- 要不要把報表交給使用者以外的人看，先問。
- 中間產物（`cands.json`、`b*.json`）留在暫存目錄就好，不要進 git。

## 不屬於這個 skill 的事

- **自動群發。**發訊息與送出招募事件（`send_message`、`send_inquiry`、`send_interview_invite`、
  `send_thank_you_letter`、`send_start_date_reminder`、`send_personality_test_invite`）在範圍內，
  但只能是步驟 8 那個形狀：使用者決定聯絡誰，逐位看過完整文字，一次一位。它送出去收不回來、
  沒有 dry-run、而且會消耗每日發送上限；使用者沒點名的人不要送，「幫我全部發出去」也不是
  可以直接照辦的指令——先把名單和文字列給他。
- **職缺文案與刊登策略。**`update_job`、`create_job` 等工具只是入口，寫什麼內容、開關哪個職缺是
  使用者的判斷；這個 skill 只負責讓你知道有這些工具、以及呼叫前要讓真人確認。
- **mcp104 自己的程式或設定。**那是維護這個 MCP server 的工作，不是用它找人。

## 參考檔案

- `references/screening-method.md` — 怎麼把「我要什麼樣的人」變成可重現的篩選規則
- `references/record-schema.md` — 履歷記錄的 JSON 格式與 `report_config.json` 設定
- `references/mcp104-notes.md` — 配額、ID 種類、已知地雷，含聯絡紀錄、範本與職缺寫入工具的陷阱。**第一次用這個 skill 一定要讀**

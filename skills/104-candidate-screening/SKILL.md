---
name: 104-candidate-screening
description: 用 mcp104（104人力銀行 MCP server）替職缺找人、篩人，並把候選人整理成一份可離線閱讀的 HTML 履歷報表（含照片、履歷附件、完整履歷原文）。Use this for ANY step of working with 104 candidates, not only the first one — 看推薦履歷或配對人選、幫某個職缺找人、依條件篩履歷、抓候選人的大頭照與履歷附件、比較幾個人選的經歷與期望薪資、把已經篩好的名單做成 HTML 報表給主管看。It applies when the request is small（「看一下今天的推薦履歷」）, and also mid-workflow, when the user already has a shortlist and never repeats the word「104」because the candidates obviously came from there — 例如「把那四十個人做成一個 html，完整履歷都要在裡面」或「這幾個人選幫我比一下經歷跟期望薪資」。Not for editing mcp104's own code or config, posting a job, or messaging candidates. Carries the quota-safe call order (only get_resume_detail costs the 300/day budget), the résumé record schema, the screening-group method, and a bundled renderer.
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

先搞懂 104 給的三種名單是怎麼來的——它們不是同一份資料的三種排序，是三個**不同的產生
機制**，決定了什麼時候該用哪一個：

| 名單 | 從哪裡來 | 工具 |
|---|---|---|
| **推薦** | 104 拿**你貼的職缺內容**去比對履歷庫，自動產生。職缺設好就有，你不用做任何事 | `list_recommended_resumes(jobno)` |
| **配對** | 104 拿**你在後台存好的搜尋條件**去比對，而且**每天更新**。條件是你自己設的，所以通常比推薦準 | `list_matched_resumes(jobno)` |
| **搜尋** | 你當下丟關鍵字即時查，跟職缺設定無關 | `search_resumes(...)` |

實務上的取捨：**推薦量大但雜**（演算法猜的），**配對量少但準**（你自己定義的條件）。
要廣就從推薦撈，要快就先看配對。

**配對可以只看今天新增的**：`list_matched_resumes(jobno, update_date_type="2")` 就是
104 網頁上那個「當日配對」分頁。這是每天回頭看有沒有新人選最省的方式——不必重篩整池。
沒帶這個參數就是全部。

⚠ 當日配對零筆時，104 回的是 `resumes: []` 加上 `pageInfo: null`（鍵在、值是 null），
不是錯誤。這個形狀會讓假設 `pageInfo` 一定存在的程式炸掉，看到它就是「今天沒有新的」。

⚠ **關閉中的職缺，兩條路由行為不一樣**：`switch="off"` 的職缺在**配對**路由回 404、
在**推薦**路由回成功加空陣列。所以某個職缺配對查不到時，先確認它是不是已經關了，
不要當成「今天沒人」。

這三條都**不扣履歷瀏覽配額**（repo 的 `docs/104-site-facts.md` §6b.4 有單一變因的對照
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

## 候選人資料怎麼處理

這些是真人的姓名、電話、Email、照片和完整工作史。

- 報表和附件**只寫本機檔案**。不要發布成 Artifact、不要上傳到任何外部服務、不要放進會被
  索引的地方。使用者要不要分享是他的決定，不是你的。
- 要不要把報表交給使用者以外的人看，先問。
- 中間產物（`cands.json`、`b*.json`）留在暫存目錄就好，不要進 git。

## 不屬於這個 skill 的事

發訊息（`send_inquiry`、`send_message`）**不在範圍內**。它送出去收不回來、沒有 dry-run、
而且會消耗每日發送上限。人選名單出來之後，要不要聯絡、聯絡誰、用什麼措辭，是使用者的決定
——把名單和建議給他，讓他說要發哪幾位，然後逐位確認再送。

## 參考檔案

- `references/screening-method.md` — 怎麼把「我要什麼樣的人」變成可重現的篩選規則
- `references/record-schema.md` — 履歷記錄的 JSON 格式與 `report_config.json` 設定
- `references/mcp104-notes.md` — 配額、ID 種類、已知地雷。**第一次用這個 skill 一定要讀**

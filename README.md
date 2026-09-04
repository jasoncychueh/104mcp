# 104-mcp

[![PyPI](https://img.shields.io/pypi/v/mcp104)](https://pypi.org/project/mcp104/)
[![Python](https://img.shields.io/pypi/pyversions/mcp104)](https://pypi.org/project/mcp104/)
[![License: MIT](https://img.shields.io/github/license/jasoncychueh/104mcp)](LICENSE)
[![GitHub](https://img.shields.io/github/stars/jasoncychueh/104mcp?style=social)](https://github.com/jasoncychueh/104mcp)

讓 AI Agent（Claude Code 或任何 MCP client）操作 **104 人力銀行企業徵才帳號**
的 MCP server：搜尋履歷、列出職缺、讀取與發送站內信、送出「詢問意願」邀約、
記錄候選人聯繫狀態。

帳號密碼**不會經過 AI 的對話內容**。登入時 Agent 只拿到一個網址，由你本人在
一個只綁定本機（`127.0.0.1`）的頁面裡完成登入，包含 104 每次都會要求的手機
驗證碼（MFA）。登入之外的所有操作都直接呼叫 104 的 JSON API，不需要開瀏覽器。

---

## 安裝

只要三步，照著做即可。以 Claude Code 為例，其他 MCP client 的設定檔格式不同，
但欄位相同。

1. 安裝 [uv](https://docs.astral.sh/uv/)。

   Windows（PowerShell）：

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   macOS／Linux：

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. 在專案根目錄的 `.mcp.json` 加入：

   ```json
   {
     "mcpServers": {
       "104-mcp": {
         "command": "uvx",
         "args": ["mcp104"],
         "timeout": 120000
       }
     }
   }
   ```

   不需要任何環境變數。`timeout` 是單一工具呼叫的上限（毫秒），120 秒是本專案建議
   的值，請照抄。

   想用 GitHub 上最新、尚未發布到 PyPI 的版本，`args` 改成
   `["--from", "git+https://github.com/jasoncychueh/104mcp.git", "mcp104"]`。

3. 啟動 Claude Code，請 Agent 呼叫 `login()`，在跳出的瀏覽器視窗完成 104 登入。
   第一次登入會自動下載一份專用的 Chromium（約 300 MB），不需要自己安裝任何東西。

### 常見狀況

- **第一次連線逾時**：Claude Code 給 MCP server 30 秒啟動時間，`uvx` 第一次要
  下載並建置套件，可能超過 30 秒。用 `/mcp` 重新連線一次即可，之後約 1 秒就能啟動。
- **更新到新版**：`uvx` 會用快取，不會自動抓新版。跑一次
  `uvx --refresh mcp104`（GitHub 版是 `uvx --refresh --from git+https://github.com/jasoncychueh/104mcp.git mcp104`）。
- **換帳號**：`logout()` 再 `login()` 用另一個 104 帳號登入即可。候選人狀態和每日發送
  計數是依 104 回報的登入 email 分開記的，兩個帳號不會混在一起。

---

## 登入流程

104 的登入每次都會觸發手機驗證碼（因為每次都是全新的瀏覽器），所以沒有辦法
自動登入，一定要你本人操作一次。流程如下：

1. Agent 呼叫 `login()`。伺服器會用你的預設瀏覽器打開登入頁（同一台機器時），
   Agent 也會告訴你網址。
2. 你在那個頁面裡輸入帳密、手機驗證碼。畫面是即時串流的 104 登入頁，滑鼠鍵盤
   都照常用。
3. 有兩個地方需要你手動點一下，Agent 看不出差別：
   - **產品選擇頁**：帳號底下有多個 104 產品時，請點「招募管理」。
   - **「此帳號已登入」對話框**：同一帳號已在別處登入時，請選「將目前帳號登出，
     立即登入」。
4. 登入完成後頁面會顯示「登入已完成」並嘗試自動關閉（瀏覽器不允許的話請自行關掉）。
   Agent 會自己等到這一刻，不需要你回頭告訴它「登入了」。

完整走完一次（含驗證碼與上面兩個分支）實測約 4–5 分鐘。登入憑證會存在本機資料
目錄，之後重開 Claude Code 不需要再登入，直到 104 那邊的登入過期為止。

---

## 工具一覽

完整參數與回傳格式請看各工具自己的說明；這裡只列一句話用途。

**登入**
- `login()` — 開始一次登入，或在憑證仍有效時直接回報已登入
- `check_login(token, wait_seconds)` — 等待一次登入完成（Agent 用，一般不需自己呼叫）
- `logout()` — 清除本機登入憑證

**履歷與職缺**
- `search_resumes(keyword, filters, page)` — 依關鍵字與篩選條件搜尋履歷
- `list_recommended_resumes(jobno, page)` — 列出 104 推薦給某職缺的履歷
- `list_matched_resumes(jobno, page, update_date_type)` — 列出配對到某職缺的履歷；`update_date_type="2"` 只看當日配對
- `get_resume_detail(candidate_id)` — 取得單一候選人的完整履歷
- `list_jobs(page)` — 列出這個帳號底下的職缺
- `get_candidate_photo(candidate_id)` — 把候選人的大頭照下載到本機並回傳路徑（沒放照片時回 `photo: null`，那是正常狀態）
- `get_resume_attachment(candidate_id, sort)` — 把履歷附件（作品集、證照掃描檔、另一版履歷 PDF……）下載到本機並回傳路徑

**篩選與欄位說明（純本機資料，不呼叫 104）**
- `browse_filter_values(key, code)` — 查 `search_resumes` 某個篩選鍵接受哪些值
- `describe_result_fields(row_type)` — 說明各工具回傳的每個欄位是什麼意思

**訊息**
- `read_messages(page, job_nos, candidate_name)` — 讀取收件匣
- `get_conversation(job_id, candidate_id, page)` — 讀取一段對話
- `send_message(job_id, candidate_id, message)` — 發送一則純文字站內信
- `send_inquiry(job_id, candidate_id, message, template_id)` — 送出「詢問意願」邀約
- `list_templates(type_id, page)` — 列出帳號裡存好的罐頭信範本

**候選人狀態（存在本機，與 104 無關）**
- `check_already_contacted(candidate_id, id_source)` — 查是否已聯繫過
- `update_candidate_status(candidate_id, id_source, status)` — 更新狀態標記

---

## 環境變數

全部選填，一般使用不需要設定任何一個。

| 變數 | 必填／預設 | 說明 |
|---|---|---|
| `MCP104_DATA_DIR` | 選填，預設依平台慣例放在使用者目錄下的 `mcp104` | 資料庫、登入憑證、節流狀態都存在這裡 |
| `MCP104_AUTH_BASE_URL` | 選填，須與 `MCP104_AUTH_BIND_PORT` 成對設定 | 登入頁的對外網址，你和伺服器不在同一台機器時才需要 |
| `MCP104_AUTH_BIND_PORT` | 選填，須與 `MCP104_AUTH_BASE_URL` 成對設定 | 登入頁監聽的埠 |
| `MAX_DAILY_MESSAGES` | `50` | 每個帳號每天最多發幾則訊息（`send_message` 與 `send_inquiry` 合計） |
| `LOGIN_TIMEOUT_SECONDS` | `900` | 等你完成登入的上限秒數 |
| `MAX_REQUESTS_PER_HOUR` | `300` | 每小時對 104 的請求上限 |
| `MAX_INLINE_WAIT_SECONDS` | `20` | 節流等待超過這個秒數就改回覆「稍後再試」 |
| `ACTIVITY_STREAK_LIMIT_MINUTES` | `20` | 連續操作多久後強制休息 |
| `REST_DURATION_MINUTES` | `3` | 強制休息的長度 |
| `MIN_CALL_INTERVAL_SECONDS` | `5` | 兩次呼叫之間的最小間隔 |

---

## 安全與節流

- **每一次發送都會真的送到一位求職者手上**，無法撤回、沒有試發模式。請要求 Agent
  先把收件人與完整內文給你看過再發。
- **每日發送上限 50 則**，`send_message` 與 `send_inquiry` 共用計數。
- **請求節流**：兩次呼叫至少間隔 5 秒、每小時最多 300 次、連續操作 20 分鐘後休息
  3 分鐘，模擬真人節奏，降低被 104 判定為機器人的風險。
- **104 自己的履歷瀏覽上限**：每天 300 份，由 104 執行；接近上限時工具會提醒。
- **遇到 Cloudflare 機器人驗證**：工具會回傳明確錯誤並要求暫停至少 1 小時。這不是
  「查無資料」，換關鍵字重試只會讓封鎖更嚴重。
- **不送已讀回報**：Agent 讀訊息不會讓對方看到「已讀」，這是刻意的。
- **候選人的照片與附件會寫進本機資料目錄**：`get_candidate_photo`／
  `get_resume_attachment` 把檔案存到資料目錄的 `resume-files/` 底下再把路徑交給
  Agent。**那些是候選人的個人資料。** 它們會在 **24 小時後自動清除**，呼叫
  `logout()` 則立即清除整個目錄；單檔上限 32 MB。這些規則只在本套件自己的程式碼裡
  成立——你自己複製走的檔案、備份軟體同步走的檔案，本套件管不到。
- **登入憑證存在本機資料目錄**，請不要把那個目錄下的檔案交給任何人或提交進版本控制。

---

## 免責聲明

這是非官方專案，與 104 人力銀行沒有任何關係，未經其授權或背書。請只在你有權操作的
帳號上使用，並遵守 104 的服務條款；透過本工具送出的每一則訊息、每一次邀約，後果由
操作者自行負責。

回報問題請到 [GitHub Issues](https://github.com/jasoncychueh/104mcp/issues)。

## 授權

MIT，見 [LICENSE](LICENSE)。

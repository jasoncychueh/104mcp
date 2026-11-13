# mcp104 操作筆記

第一次跑這個流程前先讀完。底下每一條都對應一個實際踩過的坑，不是預防性提醒。

## 1. 兩組 ID 不能互換

104 對同一個人有兩個識別碼，履歷列（含 `list_applicants` 的應徵者列）上兩個都有：

| 欄位 | 是 104 的什麼 | 給誰用 |
|---|---|---|
| `candidate_id` | `idNo` | `get_resume_detail`、`get_candidate_photo`、`get_resume_attachment` |
| `p_id` | `pId` | 訊息系統（`send_message`、`send_inquiry`、`send_interview_invite`、`send_thank_you_letter`、`send_start_date_reminder`、`send_personality_test_invite`、`get_conversation`） |

傳錯的症狀不是明確的錯誤訊息，是查無此人。清單列上兩個欄位並排放著，複製時很容易拿錯，
**把它們分開存**（例如記錄裡同時保留 `cid` 和 `pid` 兩個鍵）比事後對照省事。

注意 `read_messages` 回傳的對話列上，「`candidate_id`」這個欄位名底下裝的其實是 `pId`
——同名不同義。跨路由拿欄位前先確認來源。

## 2. 配額：只有履歷詳情要錢

104 每天 300 筆履歷瀏覽上限（`browse_limit.resume_max`），`browse_limit.on_that_day_count`
是已用量。

- **清單類呼叫不扣**：`search_resumes`、`list_recommended_resumes`、`list_matched_resumes`
  三個都是零配額（repo `docs/104-site-facts.md` §6b.4 有單一變因的對照實驗，而且推翻過一次
  相反的結論，值得一讀）。`list_applicants`（某職缺近 60 天的應徵者）同樣不扣，而且讀清單
  不會把應徵者標成已讀。
- **`get_resume_detail` 每次 +1。**
- **同一個人當天重複讀不再扣，隔天再讀會再扣**——它是「每人每日」不是「每人永久」。

  2026-09-05 實測，同一個 session：對當天已讀過詳情的 7 位候選人連續呼叫 5 次
  `get_resume_attachment`（每次內含一次履歷詳情），`on_that_day_count` 從頭到尾停在 7；
  同一天對 33 位**前一天**讀過的候選人呼叫 `get_candidate_photo`，計數 7 → 40，一位一格。
  兩組觀察在同一天、同一個帳號、只差「當天是否讀過」這一個變因。

**配額的 key 是（候選人，日期），不是候選人。** 這句話值得單獨記住：一份昨天讀過的履歷，
今天再碰一次就是一格全價。工具說明與 repo 文件在 0.3.1 以前寫的是「同一位候選人重複讀不
再扣」，沒有「當天」這個限定詞——那句話跨過午夜就是錯的。

實務後果只有一條，但很值錢：**照片、附件、薪資確認這些需要履歷詳情的動作，全部排在讀完
履歷的同一天做完。** 當天做是 0 格；隔天補抓 40 個人的照片是 40 格，一天配額的 13% 無聲
蒸發。

所以「今天先篩完，照片明天再說」不是省事，是加倍付錢。跨夜的篩選作業要嘛當天收尾，要嘛
接受隔天重新計費——不要以為名單已經在手上就不用再付了。

`on_that_day_count` 在清單路由回的是**字串**、在詳情路由回的是**數字**。要做門檻比較先
`int()`。

## 3. 沒放照片的人：104 給的是預設剪影，不是 404

104 在候選人沒放大頭照時，回的不是 404、不是空 body，是一張固定的灰色人形剪影。

**0.3.1 起 `get_candidate_photo` 自己會認出它**，回 `photo: null` 並在 warnings 說明是
怎麼認出來的（兩道閘：網址落在 `static.104.com.tw` 就不下載；下載了才發現位元組與已量測
的預設圖相同就不落地）。0.3.0 以前這兩張圖會被當成正常照片回傳，warnings 裡沒有任何提示
——40 位樣本中出現 2 次。

`build_report.py` 另外還留了一道自己的 md5 比對：

```
format : png    ← 真實照片全部是 jpeg
bytes  : 894
md5    : d388efdd9ec6e9a242753f2fdedd90c6
```

這道規則擋的是**不經工具拿到的檔案**（舊版抓下來的、別的來源複製進資料夾的），不是重複
工具的工作。用 0.3.1 以上的話正常情況下它不會命中。

**兩邊都是釘死的摘要值，104 換圖那天會同時失效**，而且兩邊用的是**不同的演算法**：

| 位置 | 常數 | 演算法 | 角色 |
|---|---|---|---|
| MCP server `src/mcp104/tools/resume_files.py` | `_PLACEHOLDER_PHOTO_SHA256` | sha256 | **規則的擁有者** |
| 本技能 `scripts/build_report.py` | `PLACEHOLDER_MD5` | md5 | 冗餘的第二道網（見上） |

這是明知的取捨，不是疏漏——失效的症狀是報表裡出現一張很糊的灰影，看到就**兩個都要更新**
（腳本這邊補 md5、工具那邊補 sha256），只改一個會讓工具與報表對「有沒有照片」講出相反的
話。**不要改用檔案大小當門檻**：894 這個數字會隨著換圖而變，「它是一個固定檔」這個性質
才是穩定的。

工具端還有一道 **hostname 閘**：照片網址落在 `static.104.com.tw` 就直接回 `photo: null`，
連資產請求都不發（省一個請求）。那道不吃摘要值，換圖也不會失效；位元組閘則是抓回來才認出
來，那條路徑花兩個請求但一樣不落地。

兩道閘的 `warnings[0]` 措辭不同，但**都以「這位候選人沒有放大頭照：」開頭**——要判斷「這人
沒照片」比對這個開頭就好，不必分辨是哪一道閘擋下的。

## 4. 列附件清單：用不存在的 sort 去問

`get_resume_attachment(candidate_id, sort=999)` 在 sort 不存在時，錯誤回應會附上完整的
`available: [{sort, title}, ...]`。

這是查「這份履歷有哪些附件」最省的方式。另一條路是重讀 `get_resume_detail` 去看 `attach_arr`
——同樣不多扣配額（當天已讀過），但會把整份履歷再倒進 context 一次。四十份履歷這樣做，
context 就沒了。

附件只認得 jpeg / png / pdf。遇到其他型別會被拒絕並回報前 8 個 magic bytes——那個簽名值得
回報給 repo，那就是補上該型別所需要的量測。

## 5. 檔案是暫存的

照片和附件落在資料目錄的 `resume-files/` 底下，**24 小時後自動清除，`logout()` 立刻清除**。

拿到就複製到輸出資料夾。「等全部抓完再一起搬」在跨夜的工作裡會整批消失。

## 6. 單一登入：任何一邊登入都會把另一邊踢掉

104 企業帳號同時只允許一個 session。使用者在瀏覽器登入會把 MCP 踢掉，MCP 登入會把使用者
踢掉，雙向的。

所以：**流程跑到一半不要叫使用者「自己去 104 看一下」**，那會中斷你手上的作業，而且他看完
你還要重登。要給他看的東西，做成檔案給他。

如果同一個帳號會被多個 agent 或多個 session 用，這件事沒有技術解，只能開子帳號。

## 7. Windows 上的雜項

- 印中文前先 `export PYTHONIOENCODING=utf-8`，否則 cp950 會在簡體字上丟 `UnicodeEncodeError`。
- Git Bash 的 `/c/...` 路徑 **Python 看不懂**。傳給 Python 的路徑用 `C:\...` 形式。
- 大段中文內容用 Write 工具寫檔，不要用 shell heredoc 或 `python -c "..."`：反斜線會被吃掉，
  而且長內容容易在引號配對上炸掉。

## 8. 清單呼叫回來之後

**篩選可以從應徵者開始。** 使用者說「看看有誰投了這個職缺」時，名單來源是
`list_applicants(jobno)`，不是推薦或配對：那是已經主動投遞的人，不是 104 猜的或條件比對
出來的。視窗固定是近 60 天；每列的 `input_date` 是投遞日（`null` 是 104 的「今天」組，
104 不給那一組的日期）。後續一樣：`candidate_id` 給 `get_resume_detail`，`p_id` 直接給訊息
工具。`list_jobs` 每列的 `apply_count60` 可以先看哪個職缺有人投，兩個配對數是 `null` 時
代表沒查到，不是 0。

`list_*` 的分頁要翻完。使用者說「看推薦履歷」通常是指整池，不是第一頁、也不是只有今天新增
的——問清楚是「今天的」還是「全部」，這兩個數字可能差一個數量級。

**拿不到候選人的 104 網頁網址，而且那是刻意的。** 104 的搜尋路由本身會送 `master_url`
（履歷頁網址），但 mcp104 從 0.2.2 起就不轉出來了——那個連結只有 MCP 這個行程手上的登入
開得了，Agent 面前的真人點不開。想繞路也繞不掉：`search_resumes` 只吃關鍵字，沒辦法用
id 反查特定的人。

要把履歷呈現給人看就讀 `get_resume_detail` 落成檔案，要圖檔就用 `get_candidate_photo`／
`get_resume_attachment`。這條原則跟第 6 點是同一件事：**能交付給使用者的只有檔案，不是
連結。**

> ⚠ 寫這份筆記時值得記住的分工：`docs/104-site-facts.md` 描述的是**104 送什麼**，工具
> 說明描述的是**mcp104 轉出什麼**，這兩者從 0.2.2 起就在刻意分岔。照 facts doc 寫 skill
> 會一直撿到工具其實丟掉的欄位——`master_url` 就是我自己踩到的那一個。**講到列上有什麼
> 欄位時，權威是工具說明，不是 facts doc。**

## 9. 本機聯絡紀錄（狀態工具）

`check_already_contacted`、`get_candidate_status`、`list_tracked_candidates`、
`update_candidate_status` 都只碰本機 SQLite，不呼叫 104。

- **`id_source` 看 id 從哪來**：履歷列／履歷詳情的 `candidate_id`（idNo）是 `"resume"`，
  `read_messages` 列的 `candidate_id`、履歷列與應徵者列的 `p_id`（pId）是 `"message"`。填錯
  不會報錯，只是查不到。
- **拿哪一個 id 查都行，但對應要先被觀察到。** 本機只在 104 把兩個 id 放在同一筆資料上交出時
  （履歷列、應徵者列、履歷詳情、事件工具的反向換算）才記下這組對應。還沒讀過任何帶兩個 id 的
  資料就拿 `candidate_id` 查，找不到當初用 `p_id` 寫下的狀態。
- **「沒有紀錄」不是「沒聯絡過」。** 它只反映這個 Agent 經由工具送出、或用
  `update_candidate_status` 寫下的狀態；別人在 104 後台發的訊息不會出現。要看 104 那一側，
  用 `read_messages(job_nos=[jobno])`。
- **`unknown` 來源的紀錄**是早期版本記下的，當時沒記 id 來自哪個系統；它是靠 id 字串相同被
  找到的，不保證是同一個人。`warnings` 會點名有幾筆。
- **同一個人的紀錄狀態不同時**，工具照列並在 `warnings` 附各筆的狀態與時間，不替你挑。
  從 `contacted` 走到 `scheduled` 是正常進展，看 `status_updated_at` 判斷哪筆較新。
- **`list_tracked_candidates` 的一列是一筆紀錄，不是一個人。**同一個人在兩個 id 下各有狀態會
  出現兩次；用 `status` 篩選時，要看每列 `linked_ids` 裡對方 id 的 `status`——他可能在另一個
  id 下已經走得更遠。一頁固定 50 筆。
- 時間都是台北時間（ISO 8601，帶 `+08:00`）；`status_updated_at` 為 `null` 是狀態寫下的時間
  不可考。

## 10. 範本與送出

- **`#jname#`** 是 104 唯一的佔位符（職缺名稱）。送出工具原樣送出文字，104 會不會在伺服器端替換
  **沒量測過**，所以送出前由 Agent 自己換成真正的職缺名稱，真人看的也是換好的文字。
- **`list_templates` 回的 `description` 就是完整本文**，不需要另外查單則範本。回傳是分頁的，
  `total_pages` 大於 `page` 時 `warnings` 會提醒。
- **範本的 `type_id` 只是歸檔分類，不換路由。**事件種類由呼叫哪個送出工具決定；範本類別與工具
  不符時 104 怎麼處理沒量測，所以挑同類別的範本。送出的永遠是 `message`，不是範本本文。
- **`update_template` 回傳的 `template.type_desc` 恆為 `null`**（單筆讀取的路由不回類別名稱），
  要類別名稱請用 `list_templates`。
- **`create_template` 在該類別的範本已滿一頁（或超過一頁）時會在送出前拒絕**，請使用者到 104
  後台新增；標題可以與既有範本重複，104 不檢查。標題 ≤ 16 字、內容 ≤ 1000 字，以 UTF-16 編碼
  單位計，emoji 算 2。
- **`update_template` 不改帶附件的範本**（送錯會讓附件消失且無法復原）；104 存下的文字與送出的
  只差空白或換行時結果是 `unconfirmed`。
- **`delete_template` 被職缺使用的範本 104 不讓刪**，錯誤會附 `used_by_jobs`（有的話）。
- 三個範本寫入工具寫的是帳號自己的文字，**不送給任何求職者、不寫發送計數與候選人狀態、不受每日
  訊息上限管**。結果一律由寫入後重讀決定，`unconfirmed` 時去後台確認、不要重送。

## 11. 職缺寫入工具

`refresh_jobs`、`close_jobs`、`open_jobs`、`delete_job`、`update_job`、`create_job` 都是**立刻
生效、求職者看得到、無法撤回**的寫入，呼叫前先讓真人確認。

- **成本是請求名額**（每小時上限 300 個請求，不扣履歷配額）：`refresh_jobs` 3、`close_jobs`／
  `open_jobs` 至多 4、`delete_job` 至多 5、`update_job` 預覽 3／送出 5、`create_job` 預覽 4／
  送出 6。預覽也是真的 104 請求。
- **寫入請求本身的回應大多沒量測過**，所以「成功」只看寫入後重讀：`"confirmed"` 是重讀對上了，
  `"unconfirmed"` 是看不出來，兩者都不是叫你重送的理由。
- **`update_job` 的 `saved: "mismatch"`**：`mismatched_fields.damaged` 非空代表沒要改的欄位也
  變了，也就是 104 把職缺改壞了，**立刻請使用者到 104 後台檢查**。禁用詞的「提醒」可以經真人確認
  後用 `accept_forbidden_word_warnings` 放行，「封鎖」不能略過。有加值廣告、自動關閉日、預約開啟
  日或「職務確認中」的職缺，請到後台改。
- **`close_jobs`** 的 `reasons` 必填；關閉前若預檢查回報有加值廣告，關閉會一併歸還並結束它們，
  要真人確認後才給 `return_add_ons=true`。關掉最後一個開放中的職缺會讓公司廣告刊期暫停，同樣
  要真人確認後才給 `allow_pausing_company_ads=true`。
- **`open_jobs`** 會用掉刊登額度並取消加值廣告預約（額度怎麼扣沒量測）；只開一個職缺而被拒絕
  時回的 `refusal` 會說是內容驗證、保障方案、沒有剩餘職務數還是數量上限。
- **`delete_job`** 只限已關閉（`switch="off"`）且 `list_jobs` 標示可刪除的職缺，一併刪除應徵
  履歷與聯絡紀錄。
- **`create_job`** 只能從既有職缺複製；**逾時後不要重送**，先用 `list_jobs` 看新職缺有沒有出現，
  不然會多出第二個公開職缺。新職缺的開關以回傳的 `new_job_switch` 為準。

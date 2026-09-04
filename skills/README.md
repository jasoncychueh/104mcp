# Skills

給 Claude Code（或其他支援 Agent Skills 的 client）用的技能包。它們不是 MCP server 的一
部分，也不會隨 `pip install mcp104` 安裝——MCP server 提供的是「能對 104 做什麼」，這裡
放的是「拿這些工具做一件完整的事該怎麼做」。

## 安裝

複製到 Claude Code 的 skills 目錄：

```bash
# 個人層級，任何專案都能用
cp -r skills/104-candidate-screening ~/.claude/skills/

# 或專案層級，只在該專案內生效
cp -r skills/104-candidate-screening <你的專案>/.claude/skills/
```

Windows（PowerShell）：

```powershell
Copy-Item -Recurse skills\104-candidate-screening $HOME\.claude\skills\
```

裝好之後 Claude 會自己在相關的請求上叫用它，不需要手動觸發。

## 目前有什麼

### `104-candidate-screening`

替一個職缺找人、篩人，最後產生一份可離線閱讀的 HTML 履歷報表（含照片、附件清單、完整
履歷原文、分組與篩選）。

涵蓋的東西：

- **配額安全的呼叫順序**——只有 `get_resume_detail` 會扣 104 每日 300 筆的額度，清單類
  呼叫不扣。整個流程的形狀是被這件事決定的。
- **履歷記錄的 JSON 格式**與分批寫檔的做法。
- **怎麼把用人主管的「我要什麼樣的人」變成可重現的分組與硬排除。**
- **`scripts/build_report.py`**——設定檔驅動的報表產生器，照片內嵌成 base64、單檔自足。

需要 Python 3 與 Pillow（只有處理照片時需要）。

發訊息（`send_inquiry` / `send_message`）刻意不在範圍內：送出去收不回來、沒有 dry-run，
該由人決定。

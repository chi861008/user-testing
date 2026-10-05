# 原型易用性測試（獨立網站版）

上傳 HTML 原型，AI 產生測試任務；受測者打開連結實際操作後，整理出成功率、卡關位置與改善建議。

不需要 Claude 帳號就能使用：管理者用密碼登入，受測者只要打開連結，不用登入也不用安裝。

## 檔案

| 檔案 | 用途 |
| --- | --- |
| `server.py` | 網站後端：登入、儲存原型與結果、呼叫 AI |
| `ai.py` | AI 串接，可切換 Claude API 或 OpenAI 相容服務 |
| `static/index.html` | 前端畫面（管理介面與受測畫面） |
| `Dockerfile` | 部署用 |
| `.env.example` | 設定範本 |

資料庫：有設定 `DATABASE_URL`（PostgreSQL）時使用它；沒有時使用 SQLite 檔案（`data/app.db`）。

## 設定

複製 `.env.example` 成 `.env`，填入：

- `ADMIN_PASSWORD`：管理者密碼
- `AI_PROVIDER`、`AI_API_KEY`、`AI_MODEL`：AI 服務設定
- `SECRET_KEY`（選填）：保護登入狀態的隨機字串，沒設定時會由管理密碼產生

AI 預設使用 Claude API（`AI_PROVIDER=anthropic`，模型預設 `claude-sonnet-4-6`）。要改用 OpenAI 或其他相容 OpenAI 格式的服務，把 `AI_PROVIDER` 設為 `openai`，並填 `AI_BASE_URL` 與 `AI_MODEL`。不設定 AI 也能使用，只是任務要手動建立、沒有 AI 整理發現。

AI 只在「產生任務」和「整理發現」時使用，受測者做測試不會用到。費用依各服務商計價，單次通常很低。

## 在自己電腦上試用

```bash
pip install -r requirements.txt
export ADMIN_PASSWORD=你的密碼 AI_API_KEY=你的金鑰    # Windows 用 set
python server.py
```

打開 http://127.0.0.1:8080 。這樣只有你自己的電腦連得到，外部受測者無法使用。

## 部署到網路上

外部受測者要能打開，網站必須放在有公開網址的地方。

**Vercel（推薦，免費方案即可）**

1. 用 GitHub 帳號登入 vercel.com，選 Add New → Project，匯入這個 repository
2. 在 Environment Variables 加入 `ADMIN_PASSWORD` 與 `AI_API_KEY`（要用 OpenAI 等其他服務時再加 `AI_PROVIDER`、`AI_MODEL`），按 Deploy
3. 部署完成後，到專案的 Storage 分頁建立 Neon（Postgres）資料庫並連接到這個專案，系統會自動加入 `DATABASE_URL`
4. 到 Deployments 分頁，對最新的部署選 Redeploy，讓資料庫設定生效

Vercel 本身不保存檔案，一定要做第 3 步，否則測試資料會遺失；管理畫面在沒連資料庫時會顯示提醒。之後只要更新 GitHub 上的程式碼，Vercel 會自動重新部署。

**公司伺服器（Docker）**

```bash
docker build -t usability-test .
docker run -d -p 8080:8080 -v usability-data:/data --env-file .env usability-test
```

請 IT 設定 HTTPS 網址指向這台伺服器的 8080 埠，並在 `.env` 設 `COOKIE_SECURE=1`。

**雲端平台**

可以選擇支援 Docker、可從 GitHub 自動部署的雲端平台。重點有兩個：

1. 環境變數照 `.env.example` 設定在平台的後台，不要把 `.env` 上傳到 GitHub
2. 一定要掛載「持久化磁碟」到 `/data`，否則每次重新部署，測試資料會全部消失

## 使用流程

1. 用密碼登入，在「設定任務」上傳 HTML 原型。有設定 AI 時會自動產生 3–5 個任務
2. 沒有 AI 時：按「新增任務」寫好說明，再按「錄製」，在預覽中實際操作一次，系統會自動記下預期路徑與完成條件
3. 每個任務按「試做」，確認出現「偵測到完成」，再按「儲存任務」
4. 到「邀請受測者」複製測試連結，傳給同事或餐廳夥伴；現場測試就用平板打開同一個連結
5. 在「測試結果」查看：
   - 重點摘要：完成率最低、最常誤點、最花時間的任務（不需要 AI）
   - 每個任務的成功率、完成時間、第一下點對比例、誤點與難易度
   - 熱點圖：每個人的第一下點擊，以及各畫面的點擊分布；點擊位置以「被點的元素」為基準，不同裝置也能對準
   - 可依受測者身分與裝置（手機／平板／電腦）篩選
   - 有設定 AI 時，可按「請 AI 整理發現」取得問題與建議

## 注意事項

- 原型要是單一 HTML 檔（CSS、JavaScript 內嵌），最大 4 MB
- 拿到測試連結的人都能看到原型內容，敏感的設計請注意連結的分享範圍
- 原型在受限的沙盒中執行，無法讀取網站的登入資訊；原型中連到外部網站的連結會被停用
- AI 產生的完成條件不一定都正確，正式測試前每題都要試做確認

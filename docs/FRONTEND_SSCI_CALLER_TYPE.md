# 前端任務：來電身分顯示 + SSCI 警戒線

> 對應後端變更：詐騙警報閾值從單一 `0.65` 改為依來電身分三種（contact 0.40 / non_contact 0.50 / private 0.55）。
> 前端需要做的是**純顯示**的兩件事，警報判斷仍完全由後端負責。

## 目標

1. 通話畫面顯示來電身分：**聯絡人（contact）／ 陌生號碼（non_contact）／ 未顯示來電（private）**
2. 在詐騙分數條上畫出**這通電話適用的警戒線**（三種身分警戒值不同）

## 後端提供的資料（已完成，不需等後端）

三種推播 `ssci_update`、`fraud_alert`、`safe_to_answer` 的 `data['ssci']` 內都包含以下欄位：

```json
{
  "type": "ssci_update",
  "conversation_id": "...",
  "ssci": {
    "caller_type": "private",        // "contact" | "non_contact" | "private"，可能為 null
    "scam_threshold": 0.55,          // 這通電話適用的警戒值：0.4 | 0.5 | 0.55
    "scam_probability": 0.63,
    "confidence": 0.63,
    "trigger_index": 2,
    "evidence": 0.63, "agreement": 0.9, "stability": 1.0,
    "...": "其他欄位見 PUSH_NOTIFICATION_SPEC.md"
  }
}
```

**推播頻率／時機**（影響 UI 的 null 處理）：

- `ssci_update`：每 3 輪對話一次（約每 6 句話），不是固定秒數。
- `fraud_alert` / `safe_to_answer`：整通電話只發一次 —— 通話滿 120 秒後的第一個 SSCI 觸發點。
- **第一次 `ssci_update` 到達前，前端拿不到 `caller_type` 和 `scam_threshold`** → 這段期間隱藏身分標籤與警戒線即可，UI 必須 null-safe。

## 要修改的檔案

### 1. `lib/models/fraud_models.dart` — SsciData 加兩個欄位

```dart
final String? callerType;      // 'contact' | 'non_contact' | 'private'
final double? scamThreshold;   // 0.4 | 0.5 | 0.55
```

`fromJson` 對應 key：`caller_type`、`scam_threshold`。

### 2. `lib/providers/call_provider.dart` — 解析並保存

- `ssci_update` handler（現約 L266–281）：建 `SsciData` 時帶入 `callerType`、`scamThreshold`（目前這兩個欄位被丟掉了）。
- `fraud_alert`（L283）／`safe_to_answer`（L291）handler：payload 同樣帶有完整 `data['ssci']`，目前只取 `scam_probability` —— 也要解析 ssci 更新 `_ssciData`，避免警報畫面拿不到身分／警戒線。

### 3. `lib/widgets/ssci_panel.dart` — 身分標籤

建議顯示文案與配色：

| caller_type | 顯示 | 建議色 |
|---|---|---|
| `contact` | 聯絡人 | 綠 |
| `non_contact` | 陌生號碼 | 橘/灰 |
| `private` | 未顯示來電 | 紅 |
| `null`（尚未收到） | 不顯示 | — |

### 4. `lib/pages/call_page.dart` — 分數條警戒線

- 分數是 `scam_probability * 100` 顯示，警戒線畫在 **`scamThreshold * 100`** 的位置（40 / 50 / 55）。
- `scamThreshold == null` 時不畫線。
- 既有的 `score >= 80 || isFraudAlert` 高風險樣式判斷（L385）**保留不動**，它已涵蓋警報推播。

## 重要規則（必讀）

1. **不要在前端寫死 0.40 / 0.50 / 0.55。** 一律讀 payload 的 `scam_threshold`，後端調參前端自動跟上。
2. **不要用「分數超過警戒線」自行觸發警報 UI。** 進警報狀態的唯一依據仍是 `fraud_alert` 推播——分數可能超線但後端尚未發警報（例如通話還沒滿 120 秒），兩邊自行判斷會不同步。警戒線純粹是視覺參考。

## 驗收清單

- [ ] 三種身分的來電，面板顯示正確標籤
- [ ] 警戒線位置隨身分變化（40 / 50 / 55）
- [ ] 第一次 SSCI 更新前（無資料）UI 正常、不顯示標籤與線
- [ ] `fraud_alert` / `safe_to_answer` 後，警報／安全畫面上仍能顯示身分與警戒線
- [ ] 收到 `fraud_alert` 時警報 UI 行為與現在一致

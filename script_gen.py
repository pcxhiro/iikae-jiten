#!/usr/bin/env python3
"""Gemini API（無料枠）で当日の台本を作る＋PDCA。

入力: スプレッドシートの過去ログ（テーマ・再生数つき）と蓄積された改善ルール
出力: テーマ・カード1（本音）・カード2（言い換え）・コメント文・新しい改善ルール
Gemini が失敗した場合は内蔵の予備台本（プランナー承認済みサンプル5本）から
未使用のものを出す。

台本ルールはプランナー（Gemini）の運用マニュアル『社会人の言い換え辞典』に準拠:
- カード1（20〜30字）: 社会人が心の中でつぶやく、解像度の高い弱音・本音
- カード2（35〜45字）: 「言い換え：『新しいラベル』。〇〇」の形式。
  後半の1文は『』の比喩の世界観の中だけで言い切る
- 合計60〜75字（約15秒）。説明・慰め・説教と綺麗事・精神論は完全禁止
"""
import json
import os
import time

FALLBACK_SCRIPTS = [
    {
        "theme": "雑務",
        "card1": "また私ばっかり、誰の仕事でもない雑務を拾ってる",
        "card2": "言い換え：『名もなきインフラ整備』。それが止まった瞬間、この部署は機能不全になります。",
        "comment_lead": "名前のない仕事ほど、実は現場を支えています。あなたの職場の『言い換え』も、ぜひコメントで教えてください。",
        "new_rule": "",
    },
    {
        "theme": "自己抑制",
        "card1": "本当はおかしいのに、角が立つのが怖くて笑って流した",
        "card2": "言い換え：『大人のスルー技術』。価値のない戦場に、自分の時間を1秒も使わなかっただけです。",
        "comment_lead": "流すのも、立派な技術です。あなたならこの場面、どう言い換えますか。コメントで教えてください。",
        "new_rule": "",
    },
    {
        "theme": "体調",
        "card1": "体調が重くて、いつもの半分も動けない自分が情けない",
        "card2": "言い換え：『低電力モードで正常運転中』。バッテリーが20%の日に、フル稼働させないのは機械として当然の仕様です。",
        "comment_lead": "動けない日は、仕様の範囲内です。あなたの『低電力な日』の言い換えも、コメントでお待ちしています。",
        "new_rule": "",
    },
    {
        "theme": "反省会",
        "card1": "帰り道、『あの言い方きつかったかな』と一人反省会が止まらない",
        "card2": "言い換え：『脳内業務のサービス残業』。退勤ボタンを押したあとの反省会は、すべて労働基準法違反です。",
        "comment_lead": "退勤後の反省会は、もう終わりにしましょう。あなたの一人反省会の言い換えも、コメントでどうぞ。",
        "new_rule": "",
    },
    {
        "theme": "評価",
        "card1": "同期がまた昇進した。自分は何も変わってない",
        "card2": "言い換え：『今は根を張る期間』。地上で急に伸びる木ほど、見えない場所で時間をかけて根を張っています。",
        "comment_lead": "見えない期間にも、ちゃんと育っているものがあります。あなたの今の時期の言い換えも、コメントで教えてください。",
        "new_rule": "",
    },
]

# 禁止表現（性別役割の押し付け）。台本に混入したら Gemini の出力を採用しない。
BANNED_PHRASES = ["女性ならでは", "笑顔で", "気配り上手"]
# カード2に混入したら不採用にする「説明・慰め・説教」系の言い回し。
BANNED_CARD2_PHRASES = ["なぜなら", "頑張りましょう", "がんばりましょう",
                        "ピンチはチャンス", "努力は裏切らない"]

PROMPT_TEMPLATE = """あなたは、長い会社員経験を持つベテランのコピーライターです。
縦型ショート動画シリーズ『社会人の言い換え辞典』の台本（カード2枚分）を
作成してください。このシリーズは、社会人が心の中でつぶやく弱音・本音に、
新しい言葉のラベルをパチンと貼り替える、約15秒の動画です。

# これまでの投稿と成績（時系列順）
{history}

# これまでに蓄積された改善ルール
{rules}

# あなたの仕事
1. 成績データを眺め、どんなテーマ・言い換えが伸びているか傾向を読む（PDCAの分析）。
   特にテーマタグごとの保存・シェアされやすさ（高評価・コメント数・平均視聴秒で推定）を意識する。
2. その分析を反映して、今日の新しい台本を1本作る。伸びているテーマの比率を少し上げる。
3. 傾向から言える改善ルールを1つだけ提案する（既存ルールの繰り返しは禁止。
   データから言えない場合は空文字にする）。

# 出力ルール
- 1本につき「カード1（本音）」と「カード2（言い換え）」の2枚構成。
- 合計文字数は60〜75文字程度（15秒で余白を持って聞き取れる長さ）に収める。
- カード1（20〜30文字）：社会人が心の中でつぶやく、解像度の高い弱音・ネガティブな本音。
  教科書的でなく、リアルで具体的な職場のワンシーンにする。過去のカード1と重複禁止。
- カード2（35〜45文字）：「言い換え：『〇〇』。〇〇」の形式。
  前半の『』で新しいラベル（比喩）を提示し、後半の1文はそのメタファー（世界観）を
  維持したまま言い切る。（例：『低電力モード』と言い換えたら、後半も「バッテリー」
  「仕様」など機械の言葉で統一する）
- 実在の人物名、書籍名、特定の企業名は一切出さない。

# 絶対鉄則（説明禁止・キレ味重視）
1. **解説・慰め・説教の完全禁止：**
   「なぜなら〜だからです」「あなたは優しい証拠」「頑張りましょう」といった
   理屈の説明や道徳的な慰めは一切書かないこと。
2. **世界観の統一（メタファーの維持）：**
   カード2の『』内で提示した「新しいラベル（比喩）」の世界観のまま、
   後半の1文を言い切ること。
3. **余白を残す：**
   聴いた人が「なるほど、そう捉えればいいのか」と自分で気づけるよう、
   言葉を削ぎ落としてパチンと終わらせること。
4. **綺麗事NG：**
   「ピンチはチャンス」「努力は裏切らない」といった意識高い系の精神論や
   上から目線の励ましは禁止。等身大で少し現実的な温度感を保つこと。
   （自販機の横で缶コーヒーを差し出すような、静かで少し現実的な温度感）

# ターゲットとテーマ配分
- ターゲット：現場で働く会社員（一般社員〜中堅層）。全体の約4〜5割は
  「日本の職場で働く女性が抱えやすい言語化しづらいモヤモヤ」をテーマにすること。
  ただし焦点は個人の性格ではなく「環境や構造の負荷」に置くこと。
- テーマ例：
  - 名前のない雑務・気配りの押し付け
  - 角が立つことを恐れて意見を飲み込む自己抑制
  - 悔しさや涙を「感情的」と抑え込む葛藤
  - 体調の波やペースダウンへの罪悪感
  - 手柄の横取り・評価の理不尽さ
  - 帰宅後のひとり反省会
  - 評価・出世への焦り／月曜朝の憂鬱／やりがい迷子
- **禁止表現：** 「女性ならではの〜」「笑顔で」「気配り上手」など、
  性別役割を押し付ける表現は絶対に使わないこと。

# お手本（このキレ味・文字数・温度感を厳守すること）
【テーマ】：雑務
【カード1】：「また私ばっかり、誰の仕事でもない雑務を拾ってる」
【カード2】：言い換え：『名もなきインフラ整備』。それが止まった瞬間、この部署は機能不全になります。

【テーマ】：自己抑制
【カード1】：「本当はおかしいのに、角が立つのが怖くて笑って流した」
【カード2】：言い換え：『大人のスルー技術』。価値のない戦場に、自分の時間を1秒も使わなかっただけです。

【テーマ】：体調
【カード1】：「体調が重くて、いつもの半分も動けない自分が情けない」
【カード2】：言い換え：『低電力モードで正常運転中』。バッテリーが20%の日に、フル稼働させないのは機械として当然の仕様です。

# コメント文（comment_lead）
- 投稿コメントの文。その日の本音に静かに寄り添い、視聴者が自分の『言い換え』を
  コメントしたくなる短い誘いを入れる（60字程度）。
- コメントに外部リンクは付けない。慰め・説教の言い回しも入れない。

# 出力形式（JSONのみ。前置き・コードフェンス禁止）
{{"theme": "テーマタグ（例：雑務／自己抑制／体調／反省会／評価 など短い語）",
  "card1": "...", "card2": "...", "comment_lead": "...",
  "new_rule": "..."}}
"""


def _history_text(log_rows, limit=14):
    rows = [r for r in log_rows if r.get("動画ID")][-limit:]
    if not rows:
        return "（まだ投稿データがありません）"
    lines = []
    for r in rows:
        lines.append(
            f"- テーマ「{r.get('テーマ', '')}」 カード1「{r.get('カード1', '')}」 "
            f"再生数={r.get('再生数') or '未取得'} "
            f"高評価={r.get('高評価') or '未取得'} "
            f"コメント数={r.get('コメント数') or '未取得'} "
            f"平均視聴秒={r.get('平均視聴秒') or '未取得'}")
    return "\n".join(lines)


def _rules_text(rules):
    if not rules:
        return "（まだルールはありません）"
    return "\n".join(f"- {r}" for r in rules)


def _fallback(log_rows):
    used = {r.get("カード1", "") for r in log_rows}
    for s in FALLBACK_SCRIPTS:
        if s["card1"] not in used:
            return dict(s)
    return dict(FALLBACK_SCRIPTS[0])


def _validate(data):
    for key in ("theme", "card1", "card2", "comment_lead"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"missing field: {key}")
    joined = str(data["card1"]) + str(data["card2"])
    for phrase in BANNED_PHRASES:
        if phrase in joined:
            raise ValueError(f"banned phrase in script: {phrase}")
    for phrase in BANNED_CARD2_PHRASES:
        if phrase in str(data["card2"]):
            raise ValueError(f"banned phrase in card2: {phrase}")
    if "言い換え" not in str(data["card2"]) or "『" not in str(data["card2"]):
        raise ValueError("card2 does not follow the 言い換え：『…』 format")


def generate(log_rows, rules):
    """台本 dict を返す。使用経路も print する。"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
    if not api_key:
        print("[script] GEMINI_API_KEY not set; using fallback script")
        return _fallback(log_rows)
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        prompt = PROMPT_TEMPLATE.format(
            history=_history_text(log_rows), rules=_rules_text(rules))
        res = None
        last_err = None
        # 503（混雑）など一時的な失敗に備えて最大3回まで再試行する
        for attempt in range(3):
            try:
                res = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", temperature=1.0),
                )
                break
            except Exception as e:
                last_err = e
                print(f"[script] Gemini attempt {attempt + 1} failed ({e})")
                if attempt < 2:
                    time.sleep(20 * (attempt + 1))
        if res is None:
            raise RuntimeError(f"Gemini failed after retries: {last_err}")
        data = json.loads(res.text)
        _validate(data)
        data.setdefault("new_rule", "")
        print(f"[script] generated by Gemini ({model})")
        return data
    except Exception as e:
        print(f"[script] Gemini failed ({e}); using fallback script")
        return _fallback(log_rows)

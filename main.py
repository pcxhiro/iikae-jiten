#!/usr/bin/env python3
"""『社会人の言い換え辞典』毎日の自動実行メイン。

流れ:
  1. スプレッドシートのタブを準備し、過去動画の最新統計を取り直す（PDCAの材料）
  2. Gemini が過去データ（テーマタグつき）＋改善ルールから今日の台本を作る
  3. Edge-TTS（予備 gTTS）で音声、Pillow でカード、FFmpeg で動画を合成
  4. YouTube にアップロード（#Shorts）※投稿先は2つ目の専用チャンネル
  5. トップコメント（自分の『言い換え』を誘う一言のみ。外部リンクなし）を自動投稿
     ※コメントの「固定（ピン留め）」は YouTube の仕様上 API ではできないため、
       投稿だけ行う。固定したい日は YouTube Studio で1タップで可能。
  6. ログ（テーマ列つき）と新しい改善ルールをスプレッドシートに記録

必要な環境変数（GitHub Secrets）:
  GEMINI_API_KEY, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN,
  SHEETS_REFRESH_TOKEN, SHEET_ID,
  GEMINI_MODEL（任意）, VIDEO_PRIVACY（任意）
  ※合鍵は2本制。YT_REFRESH_TOKEN は投稿用（YouTubeスコープのみ・
    新チャンネルで取得）、SHEETS_REFRESH_TOKEN は記録用
    （spreadsheetsスコープのみ・メインアカウントで取得）。
    SHEETS_REFRESH_TOKEN が未設定なら YT_REFRESH_TOKEN で代用する。
"""
import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import cards
import compose
import script_gen
import tts_voice
from sheets_api import Sheets, make_sheets_credentials
from youtube_api import YouTube, make_credentials

JST = ZoneInfo("Asia/Tokyo")
WORK = Path("work")


def env(name, default=""):
    return os.environ.get(name, default).strip()


def refresh_stats(sheets, yt, today_jst):
    """過去動画の統計を取り直してシートへ書き戻す（PDCA用）。"""
    rows = sheets.read_log()
    ids = [r["動画ID"] for r in rows if r.get("動画ID")]
    if not ids:
        return rows
    stats = yt.get_stats(ids)
    now = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    for r in rows:
        vid = r.get("動画ID")
        if not vid or vid not in stats:
            continue
        avg = r.get("平均視聴秒")
        # 平均視聴秒は2日以上たった動画だけ Analytics で取り直す（データ確定待ち）
        try:
            posted = datetime.date.fromisoformat(r["日付"])
            if (today_jst - posted).days >= 2:
                got = yt.get_avg_view_duration(vid)
                if got is not None:
                    avg = got
        except ValueError:
            pass
        s = stats[vid]
        sheets.update_stats(r["_row"], s["views"], s["likes"],
                            s["comments"], avg if avg != "" else None, now)
    return sheets.read_log()


def main():
    today = datetime.datetime.now(JST).date()
    date_str = today.isoformat()

    creds = make_credentials(env("YT_CLIENT_ID"), env("YT_CLIENT_SECRET"),
                             env("YT_REFRESH_TOKEN"))
    sheets = Sheets(make_sheets_credentials(env("YT_CLIENT_ID"),
                                            env("YT_CLIENT_SECRET")),
                    env("SHEET_ID"))
    sheets.ensure_tabs()
    yt = YouTube(creds)

    # 1) 統計の取り直し → 2) 台本生成
    log_rows = refresh_stats(sheets, yt, today)
    rules = sheets.read_rules()
    script = script_gen.generate(log_rows, rules)
    theme = script["theme"]
    c1_text, c2_text = script["card1"], script["card2"]
    print(f"[main] theme: {theme}")
    print(f"[main] card1: {c1_text}")
    print(f"[main] card2: {c2_text}")

    # 3) 音声・カード・動画
    WORK.mkdir(exist_ok=True)
    q_mp3, a_mp3 = str(WORK / "question.mp3"), str(WORK / "answer.mp3")
    engine = tts_voice.synthesize(c1_text, c2_text, q_mp3, a_mp3)
    print(f"[main] tts engine: {engine}")
    card1, card2 = str(WORK / "card1.png"), str(WORK / "card2.png")
    cards.make_cards(c1_text, c2_text, card1, card2)
    out_mp4 = str(WORK / "output.mp4")
    total = compose.compose(card1, card2, q_mp3, a_mp3, out_mp4)
    print(f"[main] video composed: {total:.1f}s")

    # 4) アップロード
    title = f"【社会人の言い換え辞典】「{c1_text}」 #Shorts"
    if len(title) > 95:
        title = title[:95]
    description = (
        f"本音。{c1_text}\n言い換えは、動画の中で。\n\n"
        "『社会人の言い換え辞典』は、仕事の弱音やモヤモヤに新しい言葉の"
        "ラベルを貼り替える、自動生成のショート動画シリーズです。\n"
        "音声はAI合成音声です。\n\n"
        "#Shorts #言い換え辞典 #社会人 #仕事"
    )
    privacy = env("VIDEO_PRIVACY", "public") or "public"
    video_id, actual_privacy = yt.upload(
        out_mp4, title, description,
        ["社会人の言い換え辞典", "言い換え", "仕事", "ショート"], privacy)
    url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[main] uploaded: {url} (privacy={actual_privacy})")

    # 5) トップコメント（自分の『言い換え』を誘う一言のみ。外部リンクなし）
    comment = script["comment_lead"]
    try:
        yt.post_comment(video_id, comment)
        print("[main] top comment posted")
    except Exception as e:
        print(f"[main] comment failed (video is live): {e}")

    # 6) ログ＋ルール記録（テーマは末尾のL列）
    sheets.append_log([date_str, c1_text, c2_text, video_id, url,
                       actual_privacy, "", "", "", "",
                       datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
                       theme])
    if script.get("new_rule", "").strip():
        sheets.append_rule(date_str, script["new_rule"].strip())
        print(f"[main] new rule: {script['new_rule'].strip()}")
    print("[main] done")


if __name__ == "__main__":
    main()

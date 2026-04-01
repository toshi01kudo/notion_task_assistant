import os
import datetime
import logging
from collections import defaultdict
from typing import Optional, Union
from dateutil.relativedelta import relativedelta
from google import genai
from dotenv import load_dotenv

# 既存モジュールのインポート
from module.notion_api import TaskDB, ReviewDB, RelatedDB, BaseNotionDB
from module.google_cal_api import GoogleCalendarAPI

load_dotenv()

# --- 設定読み込み ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_TASK_ID = os.getenv("NOTION_TASK_ID")
NOTION_PJ_ID = os.getenv("NOTION_PJ_ID")
NOTION_SPRINT_ID = os.getenv("NOTION_SPRINT_ID")
NOTION_REVIEW_DB_ID = os.getenv("NOTION_REVIEW_DATABASE_ID")
# GoogleカレンダーID（カンマ区切りで複数指定可能）
CALENDAR_IDS = os.getenv("GOOGLE_CALENDAR_IDS", "primary").split(",")
# サービスアカウントキーパス
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")


# --- ダミークラス定義 ---
class DummyRelatedDB:
    """TaskDB初期化のためのダミークラス。

    TaskDBの__init__でrelated_dbsが要求されるが、
    今回はAPI経由での取得のみを行うため、実体は不要。
    """

    def get_item_from_pd(self, *args, **kwargs):
        return None


def build_project_id_to_title(project_db: Optional[BaseNotionDB]) -> dict:
    """プロジェクトIDからタイトルへのマッピング辞書を構築します。

    Args:
        project_db: プロジェクトデータベースのインスタンス（RelatedDB等）。

    Returns:
        dict: プロジェクトIDをキー、タイトルを値とする辞書。
              構築に失敗した場合は空の辞書を返す。
    """
    project_id_to_title = {}
    if project_db and hasattr(project_db, "pd_items"):
        try:
            project_id_to_title = dict(zip(project_db.pd_items["id"], project_db.pd_items["title"]))
        except (KeyError, AttributeError) as e:
            logging.warning(f"Failed to create project ID to title mapping: {e}")
    return project_id_to_title


def get_target_quarter_range() -> tuple[datetime.date, datetime.date]:
    """現在の日付から「直前の四半期」の期間を算出します。

    実行日が属する四半期の前の四半期（3ヶ月間）の開始日と終了日を計算します。
    例: 5月実行 -> 1月1日〜3月31日

    Returns:
        tuple[datetime.date, datetime.date]: (開始日, 終了日) のタプル。
    """
    today = datetime.date.today()
    current_month = today.month
    # 現在の四半期の開始月を計算 (1, 4, 7, 10)
    quarter_start_month = 3 * ((current_month - 1) // 3) + 1
    current_quarter_start = datetime.date(today.year, quarter_start_month, 1)

    # 前の四半期の終了日 = 今期の開始日の前日
    end_date = current_quarter_start - datetime.timedelta(days=1)
    # 前の四半期の開始日 = 終了日の2ヶ月前
    start_date = end_date - relativedelta(months=2)
    start_date = start_date.replace(day=1)

    return start_date, end_date


# --- Notionブロック生成ヘルパー関数 ---


def create_heading_2(text: str) -> dict:
    """heading_2ブロックを作成します。

    Args:
        text (str): 見出しテキスト。

    Returns:
        dict: Notionブロックオブジェクト。
    """
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def create_heading_3(text: str) -> dict:
    """heading_3ブロックを作成します。

    Args:
        text (str): 見出しテキスト。

    Returns:
        dict: Notionブロックオブジェクト。
    """
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def create_bullet(text: str) -> dict:
    """bulleted_list_itemブロックを作成します。

    Args:
        text (str): リストアイテムのテキスト。

    Returns:
        dict: Notionブロックオブジェクト。
    """
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def get_calendar_display_name(cal_id: str, calendar_names: dict) -> str:
    """カレンダーの表示名を生成する。

    Args:
        cal_id (str): カレンダーID。
        calendar_names (dict): カレンダーIDをキー、カレンダー名を値とする辞書。

    Returns:
        str: カレンダーの表示名。名前が取得できた場合は「名前 (ID: xxx)」、できない場合は「ID」。
    """
    cal_name = calendar_names.get(cal_id, cal_id)
    if cal_name != cal_id:
        return f"{cal_name} (ID: {cal_id})"
    else:
        return cal_id


def format_calendar_blocks(events_by_cal: dict, calendar_names: dict) -> list:
    """カレンダーごとの予定リストブロックを作成します。

    Args:
        events_by_cal (dict): カレンダーIDをキー、イベントリストを値とする辞書。
        calendar_names (dict): カレンダーIDをキー、カレンダー名を値とする辞書。

    Returns:
        list: Notionブロックオブジェクトのリスト。
    """
    # 合計件数を計算
    total_count = sum(len(events) for events in events_by_cal.values())

    # 大見出しに合計件数を表示
    blocks = [create_heading_2(f"📅 Googleカレンダー実績 (合計: {total_count}件)")]

    for cal_id, events in events_by_cal.items():
        count = len(events)
        display_name = get_calendar_display_name(cal_id, calendar_names)
        blocks.append(create_heading_3(f"Calendar: {display_name}, {count}件"))
        if not events:
            blocks.append(create_bullet("(なし)"))
            continue

        # イベント列挙
        for ev in events:
            start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            summary = ev.get("summary", "タイトルなし")
            blocks.append(create_bullet(f"[{start}] {summary}"))

    return blocks


def format_task_blocks(tasks: list, project_db: Optional[BaseNotionDB] = None) -> list:
    """プロジェクトごとの完了タスクリストブロックを作成します。

    Notionのボードビューの代わりに、プロジェクト名を見出しとしたリスト形式で表現します。

    Args:
        tasks (list): Notionタスクオブジェクトのリスト。
        project_db (Optional[BaseNotionDB]): プロジェクトデータベースのインスタンス。
            Noneの場合、全てのプロジェクトが「未分類」として扱われる。

    Returns:
        list: Notionブロックオブジェクトのリスト。
    """
    blocks = [create_heading_2("✅ 完了タスク実績 (プロジェクト別)")]

    # プロジェクトIDからタイトルへのマッピングを事前に作成（パフォーマンス改善）
    project_id_to_title = build_project_id_to_title(project_db)

    # マッピング構築失敗を検出（project_dbが渡されているのにマッピングが空）
    mapping_failed = project_db is not None and not project_id_to_title
    if mapping_failed:
        logging.warning(
            "Project DB was provided but project ID to title mapping is empty. Projects will show as unresolved."
        )

    # プロジェクトごとに分類
    tasks_by_project = defaultdict(list)

    for task in tasks:
        props = task.get("properties", {})
        # relation型のプロジェクトプロパティから取得
        project_relation = props.get("プロジェクト", {}).get("relation", [])

        project_name = "未分類"
        if project_relation and len(project_relation) > 0:
            project_id = project_relation[0]["id"]
            # キャッシュされたマッピングから取得
            if project_id_to_title:
                if project_id in project_id_to_title:
                    project_name = project_id_to_title[project_id]
                else:
                    logging.warning(f"Project ID not found in mapping: {project_id}")
                    # マッピングに存在しないIDは「未分類」と区別できる未解決プレースホルダにする
                    project_name = f"未解決: {project_id[:8]}..."
            elif mapping_failed:
                # マッピング構築失敗時は、IDを含むプレースホルダ名を使用
                project_name = f"未解決: {project_id[:8]}..."

        tasks_by_project[project_name].append(task)

    for project_name, task_list in tasks_by_project.items():
        blocks.append(create_heading_3(f"Project: {project_name}"))
        for task in task_list:
            props = task.get("properties", {})
            title_list = props.get("Name", {}).get("title", []) or props.get("タスク名", {}).get("title", [])
            title = title_list[0]["plain_text"] if title_list else "無題"
            blocks.append(create_bullet(title))

    return blocks


def format_ai_content_blocks(markdown_text: str) -> list:
    """Geminiの生成テキストをNotionブロックに変換します。

    Args:
        markdown_text (str): AIが生成したテキスト。

    Returns:
        list: Notionブロックオブジェクトのリスト。
    """
    blocks = [create_heading_2("🤖 四半期の振り返り (AI分析)")]

    # 長文対策として2000文字ごとに分割してParagraphブロックにする
    chunk_size = 2000
    for i in range(0, len(markdown_text), chunk_size):
        chunk = markdown_text[i : i + chunk_size]
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            }
        )
    return blocks


# --- Gemini関連処理 ---


def format_data_for_ai(
    tasks: list, events_by_cal: dict, calendar_names: dict, project_db: Optional[BaseNotionDB] = None
) -> str:
    """収集したタスクとイベントデータを、AIへのプロンプト用にテキスト整形します。

    Args:
        tasks (list): Notionから取得したタスクオブジェクト(辞書)のリスト。
        events_by_cal (dict): カレンダーごとのイベントリスト辞書。
        calendar_names (dict): カレンダーIDをキー、カレンダー名を値とする辞書。
        project_db (Optional[BaseNotionDB]): プロジェクトデータベースのインスタンス。
            Noneの場合、全てのプロジェクトが「未分類」として扱われる。

    Returns:
        str: AIへの入力として利用する整形済みテキスト文字列。
    """
    # プロジェクトIDからタイトルへのマッピングを事前に作成（パフォーマンス改善）
    project_id_to_title = build_project_id_to_title(project_db)

    # マッピング構築失敗を検出（project_dbが渡されているのにマッピングが空）
    mapping_failed = project_db is not None and not project_id_to_title

    text = "【完了タスク】\n"
    for task in tasks:
        props = task.get("properties", {})
        # タイトルの取得
        title_list = props.get("Name", {}).get("title", []) or props.get("タスク名", {}).get("title", [])
        title = title_list[0]["plain_text"] if title_list else "無題"

        # プロジェクトの取得（relation型から解決）
        project_relation = props.get("プロジェクト", {}).get("relation", [])

        project = "未分類"
        if project_relation and len(project_relation) > 0:
            project_id = project_relation[0]["id"]
            # キャッシュされたマッピングから取得
            if project_id_to_title:
                if project_id in project_id_to_title:
                    project = project_id_to_title[project_id]
                else:
                    logging.warning(f"Project ID not found in mapping: {project_id}")
                    # マッピングに存在しないが relation がある場合は、未解決を明示
                    project = f"未解決: {project_id[:8]}..."
            elif mapping_failed:
                # マッピング構築失敗時は、IDを含むプレースホルダ名を使用
                project = f"未解決: {project_id[:8]}..."

        text += f"- {title} (Project: {project})\n"

    text += "\n【カレンダー予定】\n"
    for cal_id, events in events_by_cal.items():
        display_name = get_calendar_display_name(cal_id, calendar_names)
        text += f"Source: {display_name}\n"
        for ev in events:
            start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            summary = ev.get("summary", "タイトルなし")
            text += f"- [{start}] {summary}\n"
    return text


def generate_review(text_data: str, period_str: str) -> str:
    """Gemini APIを使用して、活動記録から振り返りレポートを生成します。

    Args:
        text_data (str): タスクとイベント情報を含む整形済みテキスト。
        period_str (str): 振り返り対象の期間を表す文字列。

    Returns:
        str | None: 生成された振り返りテキスト。エラー時はNoneを返す。
    """
    if not GOOGLE_API_KEY:
        print("Gemini API Key is missing.")
        return None

    client = genai.Client(api_key=GOOGLE_API_KEY)

    prompt = f"""
あなたは客観的なデータ分析官です。
以下のデータは、{period_str}の活動記録（完了タスクとカレンダーのイベント）です。
このデータを元に、四半期の活動報告レポートを作成してください。

## 指示
- **トーン&マナー:** 冷静、客観的、簡潔、ビジネスライク。感情的な表現やキャラクター性は不要です。事実を淡々と記述してください。
- **構成:** 以下の3つの観点で事実に基づいた分析を行ってください。
    1. **TRPG活動:** 実施回数、傾向、特筆すべきセッション。
    2. **サークル活動 (Luxy/T4):** 運営タスクの進捗、イベント実績。
    3. **全体総括:** その他プライベートや技術学習を含めた四半期の総評。

## 入力データ
{text_data}
    """
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return None


def initialize_related_dbs() -> (
    tuple[Union[BaseNotionDB, DummyRelatedDB], Union[BaseNotionDB, DummyRelatedDB], Optional[BaseNotionDB]]
):
    """プロジェクトDBとスプリントDBを初期化します。

    Returns:
        tuple: (project_db, sprint_db, project_db_for_format)
            - project_db: TaskDBに渡すプロジェクトDBインスタンス
            - sprint_db: TaskDBに渡すスプリントDBインスタンス
            - project_db_for_format: format関数に渡すプロジェクトDB（ダミーの場合はNone）
    """
    project_db = DummyRelatedDB()
    sprint_db = DummyRelatedDB()
    project_db_for_format = None
    project_db_init_success = False
    sprint_db_init_success = False

    if NOTION_PJ_ID:
        try:
            project_db = RelatedDB(db_id=NOTION_PJ_ID, token=NOTION_TOKEN)
            # データ取得の成功を確認（必須カラムの存在と件数チェック）
            if hasattr(project_db, "pd_items") and not project_db.pd_items.empty:
                required_columns = {"id", "title"}
                if required_columns.issubset(project_db.pd_items.columns):
                    project_db_for_format = project_db
                    project_db_init_success = True
                    print(f"プロジェクトDB: {len(project_db.pd_items)}件取得")
                else:
                    logging.error(f"プロジェクトDBに必須カラム({required_columns})が不足しています")
                    project_db = DummyRelatedDB()  # 検証失敗時はダミーに差し戻す
            else:
                logging.error("プロジェクトDBのデータ取得に失敗しました（pd_itemsが空）")
                project_db = DummyRelatedDB()  # 検証失敗時はダミーに差し戻す
        except Exception as e:
            logging.error(f"プロジェクトDBの初期化に失敗しました: {e}")
            project_db = DummyRelatedDB()

    if NOTION_SPRINT_ID:
        try:
            sprint_db = RelatedDB(db_id=NOTION_SPRINT_ID, token=NOTION_TOKEN)
            # データ取得の成功を確認
            if hasattr(sprint_db, "pd_items") and not sprint_db.pd_items.empty:
                required_columns = {"id", "title"}
                if required_columns.issubset(sprint_db.pd_items.columns):
                    sprint_db_init_success = True
                    print(f"スプリントDB: {len(sprint_db.pd_items)}件取得")
                else:
                    logging.error(f"スプリントDBに必須カラム({required_columns})が不足しています")
                    sprint_db = DummyRelatedDB()  # 検証失敗時はダミーに差し戻す
            else:
                logging.error("スプリントDBのデータ取得に失敗しました（pd_itemsが空）")
                sprint_db = DummyRelatedDB()  # 検証失敗時はダミーに差し戻す
        except Exception as e:
            logging.error(f"スプリントDBの初期化に失敗しました: {e}")
            sprint_db = DummyRelatedDB()

    # 初期化結果をサマリー出力
    if project_db_init_success and sprint_db_init_success:
        print("✓ プロジェクトDBとスプリントDBを初期化しました")
    elif project_db_init_success and not sprint_db_init_success:
        print("警告: スプリントDBのIDが未設定または初期化に失敗しました。ダミーを使用します。")
    elif sprint_db_init_success and not project_db_init_success:
        print("警告: プロジェクトDBのIDが未設定または初期化に失敗しました。ダミーを使用します。")
    else:
        print("警告: プロジェクトDBおよびスプリントDBが未設定または初期化に失敗しました。両方ともダミーを使用します。")

    return project_db, sprint_db, project_db_for_format


def fetch_completed_tasks(
    start_date: datetime.date,
    end_date: datetime.date,
    project_db: Union[BaseNotionDB, DummyRelatedDB],
    sprint_db: Union[BaseNotionDB, DummyRelatedDB],
) -> list:
    """指定期間の完了タスクを取得します。

    Args:
        start_date: 期間開始日
        end_date: 期間終了日
        project_db: プロジェクトDBインスタンス
        sprint_db: スプリントDBインスタンス

    Returns:
        list: 完了タスクのリスト
    """
    try:
        # NOTE: TaskDBは初期化時に全件取得（_load_and_process_data）が走るが、
        # このスクリプトではget_done_tasks()のクエリのみ使用するため、
        # 将来的には初期ロードをスキップするオプションや軽量クラスの導入を検討。
        tasks_db = TaskDB(
            db_id=NOTION_TASK_ID, token=NOTION_TOKEN, related_dbs={"Projects": project_db, "Sprints": sprint_db}
        )

        # DataFrameを使わず、直接APIを叩くメソッドを使用
        done_tasks = tasks_db.get_done_tasks(start_date.isoformat(), end_date.isoformat())
        print(f"Notion完了タスク: {len(done_tasks)}件取得")
        return done_tasks
    except Exception as e:
        print(f"TaskDB Init/Fetch Error: {e}")
        return []


def fetch_calendar_events(start_date: datetime.date, end_date: datetime.date) -> tuple[dict, dict]:
    """指定期間のGoogleカレンダーイベントを取得します。

    Args:
        start_date: 期間開始日
        end_date: 期間終了日

    Returns:
        tuple: (events_by_cal, calendar_names)
            - events_by_cal: カレンダーIDをキー、イベントリストを値とする辞書
            - calendar_names: カレンダーIDをキー、カレンダー名を値とする辞書
    """
    events_by_cal = {}
    calendar_names = {}

    for cal_id in CALENDAR_IDS:
        cid = cal_id.strip()
        if not cid:
            continue
        try:
            gcal = GoogleCalendarAPI(key_file_path=SERVICE_ACCOUNT_FILE, calendar_id=cid)
            cal_events = gcal.list_events(start_date, end_date)
            calendar_name = gcal.get_calendar_name()
            events_by_cal[cid] = cal_events
            calendar_names[cid] = calendar_name
            display_name = get_calendar_display_name(cid, calendar_names)
            print(f"Calendar({display_name}): {len(cal_events)}件")
        except Exception as e:
            print(f"Calendar({cid}) Skip: {e}")

    return events_by_cal, calendar_names


def create_review_page(
    period_str: str,
    done_tasks: list,
    events_by_cal: dict,
    calendar_names: dict,
    ai_review_text: str,
    project_db_for_format: Optional[BaseNotionDB],
) -> None:
    """Notionに振り返りページを作成します。

    Args:
        period_str: 期間を表す文字列
        done_tasks: 完了タスクのリスト
        events_by_cal: カレンダーイベントの辞書
        calendar_names: カレンダー名の辞書
        ai_review_text: AI生成の振り返りテキスト
        project_db_for_format: プロジェクトDBインスタンス
    """
    if not NOTION_REVIEW_DB_ID:
        print("DB ID未設定のためスキップ")
        return

    try:
        review_db = ReviewDB(db_id=NOTION_REVIEW_DB_ID, token=NOTION_TOKEN)

        # まず空のページを作成 (タイトルのみ)
        new_page = review_db.create_review_page(title=f"{period_str} 振り返りレポート", content="")

        if not new_page:
            print("ページ作成に失敗しました")
            return

        page_id = new_page["id"]
        print(f"ページ作成成功 (ID: {page_id})。詳細ブロックを追加します...")

        # ブロックリストの構築
        cal_blocks = format_calendar_blocks(events_by_cal, calendar_names)
        task_blocks = format_task_blocks(done_tasks, project_db_for_format)
        ai_blocks = format_ai_content_blocks(ai_review_text)

        # 全ブロックを結合
        all_blocks = cal_blocks + task_blocks + ai_blocks

        # ブロックを追加
        review_db.append_children(page_id, all_blocks)
        print("✅ 全ブロックの追加が完了しました！")

    except Exception as e:
        print(f"Notion Write Error: {e}")


def main():
    """四半期ごとの振り返り生成プロセスのメイン実行関数。"""
    print("--- 四半期振り返り自動生成を開始します ---")

    start_date, end_date = get_target_quarter_range()
    period_str = f"{start_date.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}"
    print(f"対象期間: {period_str}")

    # 1. プロジェクトDBとスプリントDBの初期化
    project_db, sprint_db, project_db_for_format = initialize_related_dbs()

    # 2. Notion完了タスク取得
    done_tasks = fetch_completed_tasks(start_date, end_date, project_db, sprint_db)

    # 3. Googleカレンダーイベント取得
    events_by_cal, calendar_names = fetch_calendar_events(start_date, end_date)

    # 4. Gemini分析
    if not done_tasks and not events_by_cal:
        print("データが存在しないため終了します。")
        return

    input_text = format_data_for_ai(done_tasks, events_by_cal, calendar_names, project_db_for_format)
    print("Geminiによる分析を実行中...")
    ai_review_text = generate_review(input_text, period_str)

    if not ai_review_text:
        print("AI生成失敗のため終了")
        return

    print("\n--- 生成完了。Notionに書き込みます ---")

    # 5. Notionページ作成とブロック追加
    create_review_page(period_str, done_tasks, events_by_cal, calendar_names, ai_review_text, project_db_for_format)


if __name__ == "__main__":
    main()

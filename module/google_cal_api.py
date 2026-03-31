# module/google_cal_api.py

import logging
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from typing import Dict, Any, List, Optional


class GoogleCalendarAPI:
    """
    Google Calendar APIを操作するためのラッパークラス。
    サービスアカウントを使用した認証を行う。

    Attributes:
        calendar_id (str): 操作対象のカレンダーID。
        service (Resource): Google Calendar APIのサービスリソース。
    """

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, key_file_path: str, calendar_id: str) -> None:
        """
        GoogleCalendarAPIクラスを初期化する。

        Args:
            key_file_path (str): サービスアカウントのJSONキーファイルのパス。
            calendar_id (str): 操作対象のカレンダーID (メールアドレス形式)。
        """
        self.calendar_id = calendar_id
        creds = service_account.Credentials.from_service_account_file(key_file_path, scopes=self.SCOPES)
        self.service = build("calendar", "v3", credentials=creds)

    def list_events(self, start_date: datetime.date, end_date: datetime.date) -> List[Dict[str, Any]]:
        """
        指定された期間内のイベント一覧を取得する。

        Args:
            start_date (datetime.date): 取得開始日。
            end_date (datetime.date): 取得終了日。

        Returns:
            List[Dict[str, Any]]: イベント情報の辞書リスト。イベントが見つからない場合は空リスト。
        """
        time_min = start_date.isoformat() + "T00:00:00Z"
        time_max = end_date.isoformat() + "T23:59:59Z"

        events_result = (
            self.service.events()
            .list(
                calendarId=self.calendar_id, timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy="startTime"
            )
            .execute()
        )
        return events_result.get("items", [])

    def create_event(self, title: str, start_date: datetime.date, description: str = "") -> str:
        """
        終日イベントを作成する。

        Args:
            title (str): イベントのタイトル。
            start_date (datetime.date): イベントの日付。
            description (str, optional): イベントの説明。デフォルトは空文字。

        Returns:
            str: 作成されたイベントのID。
        """
        event = {
            "summary": title,
            "description": description,
            "start": {"date": start_date.isoformat()},
            "end": {"date": (start_date + datetime.timedelta(days=1)).isoformat()},  # 終日は+1日必要
        }
        result = self.service.events().insert(calendarId=self.calendar_id, body=event).execute()
        logging.info(f"Created GCal Event: {title} ({result['id']})")
        return result["id"]

    def update_event(
        self, event_id: str, title: str, start_date: Optional[datetime.date], description: str = ""
    ) -> None:
        """
        既存のイベントを更新する。

        Args:
            event_id (str): 更新対象のイベントID。
            title (str): 新しいタイトル。
            start_date (Optional[datetime.date]): 新しい日付。Noneの場合は日付を更新しない。
            description (str, optional): 新しい説明。デフォルトは空文字。
        """
        body = {"summary": title, "description": description}

        if start_date:
            body["start"] = {"date": start_date.isoformat()}
            body["end"] = {"date": (start_date + datetime.timedelta(days=1)).isoformat()}

        self.service.events().patch(calendarId=self.calendar_id, eventId=event_id, body=body).execute()
        logging.info(f"Updated GCal Event: {title} ({event_id})")

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """
        指定されたIDのイベント詳細を取得する。

        Args:
            event_id (str): GoogleカレンダーのイベントID。

        Returns:
            Optional[Dict[str, Any]]: イベント情報。存在しない、またはエラーの場合はNone。
        """
        try:
            return self.service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except Exception:
            return None

    def get_events(
        self, start_time: datetime.datetime, end_time: datetime.datetime, calendar_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """指定された期間とカレンダーIDに基づいてイベントを取得します（日時ベース）。

        このメソッドは、`datetime.date` を受け取る `list_events()` の「日付ベース」の API に対し、
        開始・終了を `datetime.datetime` で指定したい場合のための「日時ベース」の補完的な API です。
        すなわち、日単位の範囲取得には `list_events()` を、時刻単位でより細かい期間指定が必要な場合には
        この `get_events()` を使用します。

        Args:
            start_time (datetime.datetime): 取得範囲の開始日時。
            end_time (datetime.datetime): 取得範囲の終了日時。
            calendar_id (Optional[str], optional): 取得対象のカレンダーID。
                None の場合はこのインスタンスの ``self.calendar_id`` が使用されます。
        Returns:
            List[Dict[str, Any]]: 取得したイベント情報のリスト（辞書形式）。
        """
        # calendar_id が指定されていない場合はインスタンスの calendar_id を利用する
        if calendar_id is None:
            calendar_id = self.calendar_id

        # "YYYY-MM-DDTHH:MM:SSZ" の形式に強制変換します
        time_min_str = start_time.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max_str = end_time.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        events_result = (
            self.service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min_str,
                timeMax=time_max_str,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        return events_result.get("items", [])

    def get_calendar_name(self) -> str:
        """カレンダーの表示名を取得する。

        Returns:
            str: カレンダーの表示名。取得できない場合はカレンダーIDを返す。
        """
        try:
            calendar = self.service.calendars().get(calendarId=self.calendar_id).execute()
            return calendar.get("summary", self.calendar_id)
        except Exception:
            logging.warning(
                "Failed to fetch calendar name for calendar_id=%s; falling back to calendar_id.",
                self.calendar_id,
                exc_info=True,
            )
            return self.calendar_id

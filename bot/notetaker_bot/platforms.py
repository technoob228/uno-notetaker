"""How the bot gets into each kind of call — as a guest, by the link.

One engine, a table per platform. Each entry lists what the page says or
shows at each step, as regular expressions over the accessible names of
buttons and fields (aria-label or text) and over the visible text of the
page, in English (the browser asks for en-US) and, where the service ignores
that, in Russian. Platforms change their pages: when a join breaks, this is
the file to fix — the bot saves a screenshot of what it saw (Calls page →
"What the bot saw").

Not here, on purpose: KakaoTalk (group calls live in the app, there is no
link to open in a browser).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse


@dataclass
class Spec:
    name: str
    before: list[str] = field(default_factory=list)      # click if shown: cookie banners, "continue in browser"
    name_field: list[str] = field(default_factory=list)  # the "your name" input
    fields: dict[str, str] = field(default_factory=dict)  # other inputs: label regex → value
    mute: list[str] = field(default_factory=list)        # buttons that turn our mic / camera off
    mute_switches: list[str] = field(default_factory=list)  # toggles (role switch/checkbox) to turn off
    join: list[str] = field(default_factory=list)        # the join / ask-to-join button
    lobby: list[str] = field(default_factory=list)       # text: waiting to be let in
    in_call: list[str] = field(default_factory=list)     # buttons only shown in the call (Leave)
    after_join: list[str] = field(default_factory=list)  # click once in: "Join audio by computer", popups
    denied: list[str] = field(default_factory=list)      # text: not let in / can't join
    ended: list[str] = field(default_factory=list)       # text: the call is over for us
    alone: list[str] = field(default_factory=list)       # text: nobody else is here
    chat_open: list[str] = field(default_factory=list)
    chat_input: list[str] = field(default_factory=list)
    leave: list[str] = field(default_factory=list)
    leave_confirm: list[str] = field(default_factory=list)

    def url(self, url: str) -> str:
        return url


class Meet(Spec):
    def url(self, url: str) -> str:
        return url + ("&" if "?" in url else "?") + "hl=en"


class Zoom(Spec):
    def url(self, url: str) -> str:
        """Any Zoom invite → the web client (no app, no SDK): /wc/join/<id>."""
        u = urlparse(url)
        m = re.match(r"^/(?:j|w|s|wc/join|wc)/(\d{9,12})", u.path)
        if not m:
            return url
        host = u.netloc if "zoomgov" in u.netloc else "app.zoom.us"
        q = parse_qs(u.query)
        pwd = (q.get("pwd") or [""])[0]
        return f"https://{host}/wc/join/{m.group(1)}" + (f"?pwd={pwd}" if pwd else "")


MEET = Meet(
    name="Google Meet",
    before=[r"^Got it$", r"^Dismiss$", r"^Continue without microphone and camera$", r"^Accept all$",
            r"^I understand$"],
    name_field=[r"^Your name$", r"What's your name"],
    mute=[r"^Turn off microphone", r"^Turn off camera"],
    # Meet names the button by what you join with: "Ask to join without camera" when the
    # computer has no camera (always true for the bot), so match the start only.
    join=[r"^Ask to join", r"^Join now", r"^Join anyway"],
    lobby=[r"Asking to join", r"Please wait until a meeting host brings you", r"when someone lets you in",
           r"Someone will let you in"],
    in_call=[r"^Leave call$"],
    after_join=[r"^Got it$", r"^Dismiss$", r"^Close$"],
    denied=[r"You can't join this (video )?call", r"denied your request", r"No one responded to your request",
            r"request to join .*(was )?declined", r"Check your meeting code", r"meeting code (is invalid|has expired)",
            r"Sign in to join"],
    ended=[r"You left the meeting", r"You've been removed from the (meeting|call)", r"The call has ended",
           r"This call has ended", r"Return to home screen"],
    alone=[r"You're the only one here", r"No one else is here"],
    chat_open=[r"^Chat with everyone", r"^Chat$", r"^Open chat"],
    chat_input=[r"Send a message"],
    leave=[r"^Leave call$"],
)

TEAMS = Spec(
    name="Microsoft Teams",
    before=[r"Continue on this browser", r"Join on the web instead", r"Use the web app instead",
            r"^Accept$", r"^Accept all$", r"^Dismiss$", r"^Got it$"],
    name_field=[r"Type your name", r"^Name$", r"Enter (your )?name", r"Your name"],
    mute=[r"^Turn camera off", r"^Mute microphone", r"^Turn off microphone", r"^Mute mic"],
    mute_switches=[r"^Microphone$", r"^Camera$", r"^Video$", r"^Mic$"],
    join=[r"^Join now$", r"^Join meeting$", r"^Join$"],
    lobby=[r"(should|will) let you in", r"let people know you're waiting", r"waiting for (the )?(organizer|host)",
           r"You're in the lobby", r"Hang tight"],
    in_call=[r"^Leave", r"^Hang up$"],
    after_join=[r"^Dismiss$", r"^Got it$", r"^Close$"],
    denied=[r"We can't find this meeting", r"denied access", r"you (were|have been) denied", r"couldn't (join|get you into)", r"We couldn't connect you",
            r"Nobody responded to your request", r"Your request to join (was )?declined"],
    ended=[r"The meeting has ended", r"You've been removed from (this|the) meeting", r"You left the meeting",
           r"^Meeting ended", r"Call ended", r"\bRejoin\b"],
    alone=[r"Waiting for others to join", r"You're the only one here"],
    chat_open=[r"^Chat$", r"^Show conversation", r"^Open chat"],
    chat_input=[r"Type a (new )?message", r"Type a message"],
    leave=[r"^Leave", r"^Hang up$"],
)

ZOOM = Zoom(
    name="Zoom",
    before=[r"^Accept Cookies$", r"^Accept All Cookies$", r"^I Agree$", r"^Agree$", r"^Accept$",
            r"^Join from Your Browser$", r"^Continue$"],
    name_field=[r"^Your Name$", r"^Name$", r"Enter your name"],
    mute=[r"^Mute$", r"^Stop Video$", r"^mute my microphone", r"^stop my video"],
    join=[r"^Join$"],
    lobby=[r"host will let you in", r"Waiting for the host to start", r"We've let them know you're here",
           r"waiting room", r"Please wait"],
    in_call=[r"^Leave$", r"^Leave Meeting$", r"^Leave meeting"],
    after_join=[r"Join Audio by Computer", r"Join with Computer Audio", r"^Join Audio$", r"^Got it$", r"^OK$"],
    denied=[r"passcode (is )?(wrong|incorrect|invalid)", r"Meeting Passcode", r"meeting (ID )?is not valid",
            r"Invalid meeting ID", r"has been locked", r"not (allowed|authorized) to join", r"Sign in to join",
            r"only authenticated users", r"verify (you are|that you're) (human|not a robot)", r"captcha",
            r"This meeting link is invalid"],
    ended=[r"meeting has been ended by (the )?host", r"The meeting has ended", r"You have been removed",
           r"This meeting has been ended"],
    chat_open=[r"open the chat panel", r"^Chat$"],
    chat_input=[r"Type message here", r"chat message"],
    leave=[r"^Leave$", r"^Leave Meeting$"],
    leave_confirm=[r"^Leave Meeting$", r"^Leave meeting$"],
)

TELEMOST = Spec(
    name="Yandex Telemost",
    before=[r"Продолжить в браузере", r"Continue in (the )?browser", r"Join (via|in|from) (the )?browser",
            r"Войти как гость", r"Join as (a )?guest", r"^Принять", r"^Accept", r"^Понятно$", r"^Got it$"],
    name_field=[r"Ваше имя", r"^Имя", r"Your name", r"^Name$", r"Как вас (зовут|представить)"],
    mute=[r"Выключить микрофон", r"Выключить камеру", r"Turn off (the )?(microphone|camera)", r"^Mute"],
    # Not a bare "Войти": that is "sign in to Yandex" in the page's header.
    join=[r"^Подключиться$", r"^Присоединиться$", r"^Войти во встречу$", r"^Войти в звонок$", r"^Join$",
          r"^Join (the )?(meeting|call)$", r"^Connect$"],
    lobby=[r"Ожида", r"зал ожидания", r"организатор (скоро )?впустит", r"waiting room", r"will let you in"],
    in_call=[r"Покинуть", r"Выйти из встречи", r"Leave( the)? (call|meeting)", r"^Leave$", r"Отключиться"],
    after_join=[r"^Понятно$", r"^Got it$", r"^Закрыть$", r"^Close$"],
    denied=[r"(не впустил|отклонил)", r"declined", r"denied", r"Такого звонка нет", r"Встреча не найдена",
            r"(Meeting|Call) not found", r"No such (call|meeting)", r"Ссылка (недействительна|устарела)"],
    ended=[r"Встреча (завершена|закончилась)", r"Вас удалили", r"Вы покинули", r"meeting (has )?ended",
           r"You were removed", r"You left"],
    alone=[r"Вы (здесь )?одн[иа]", r"You're the only one"],
    chat_open=[r"^Чат$", r"^Chat$"],
    chat_input=[r"Сообщение", r"Напишите", r"Message"],
    leave=[r"Покинуть", r"Выйти из встречи", r"Leave( the)? (call|meeting)", r"^Leave$", r"Отключиться"],
)

ZOHO = Spec(
    name="Zoho Meeting",
    before=[r"^Accept", r"Join (from|via|in) (the )?browser", r"Continue (in|on) (the )?browser", r"^Got it$"],
    name_field=[r"^Name$", r"Your name", r"Enter (your )?name"],
    fields={r"e-?mail": "notetaker@example.com"},
    mute=[r"^Mute", r"Turn off (mic|camera|video)", r"^Stop video", r"^Turn off"],
    join=[r"^Join$", r"^Join Now$", r"^Join Meeting$", r"^Join meeting$"],
    lobby=[r"waiting for the (host|presenter)", r"host will (admit|let) you", r"lobby", r"Please wait"],
    in_call=[r"^Leave", r"^Exit", r"Leave meeting"],
    after_join=[r"Computer audio", r"^Join audio", r"Use computer audio", r"Join with computer audio", r"^Got it$"],
    denied=[r"denied", r"declined", r"not allowed", r"(meeting|link) (is )?invalid"],
    ended=[r"meeting has ended", r"Host has ended", r"removed from the meeting", r"ended the meeting"],
    chat_open=[r"^Chat$"],
    chat_input=[r"Type (a |your )?message", r"^Message"],
    leave=[r"^Leave", r"^Exit", r"Leave meeting"],
    leave_confirm=[r"^Leave$", r"^Yes"],
)

GENERIC = Spec(
    name="Call",
    before=[r"^Accept", r"^Got it$", r"Continue (in|on) (the |this )?browser", r"Join (in|from|via) (the )?browser"],
    name_field=[r"name"],
    mute=[r"^Mute", r"^Turn off (microphone|camera)", r"^Stop (camera|video)"],
    join=[r"^Join", r"^Ask to join", r"^Connect$", r"^Подключиться", r"^Войти (во|в) (встречу|звонок)"],
    lobby=[r"Asking to join", r"lobby", r"waiting (room|for)", r"let you in", r"Ожида"],
    in_call=[r"^Leave", r"^Hang ?up", r"^End call", r"Покинуть", r"Выйти"],
    after_join=[r"^Got it$", r"^Dismiss$"],
    denied=[r"denied", r"declined", r"not allowed", r"invalid"],
    ended=[r"meeting (has )?ended", r"call (has )?ended", r"You left", r"You've been removed", r"You have been removed",
           r"Встреча завершена"],
    alone=[r"You're the only one", r"only one here"],
    chat_open=[r"^Chat$", r"^Open chat"],
    chat_input=[r"message"],
    leave=[r"^Leave", r"^Hang ?up", r"^End call", r"Покинуть", r"Выйти"],
)

SPECS = {"meet": MEET, "teams": TEAMS, "zoom": ZOOM, "telemost": TELEMOST, "zoho": ZOHO,
         "jitsi": GENERIC, "other": GENERIC}


def spec_for(platform: str) -> Spec:
    return SPECS.get(platform, GENERIC)

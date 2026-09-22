#!/usr/bin/env python3
"""Generate two-voice test meetings (RU and EN) with macOS `say` + ffmpeg.

Output (in this directory):
  dialog-ru.m4a, dialog-en.m4a     one mixed track (like an uploaded recording)
  dialog-en-mic.webm / -tab.webm (opus, like MediaRecorder)     the same EN meeting split per speaker, aligned
                                   on one timeline (like mic + tab capture)

Russian has a single system voice (Milena); the second speaker is Milena
pitched down, which is enough for a listener (and a test) to tell them apart.
"""
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

RU = [
    ("A", "Добрый день, Олег. Спасибо, что нашли время. Давайте обсудим запуск пилота для вашей команды."),
    ("B", "Здравствуйте. Да, нам важно понять сроки. Мы хотим начать до пятнадцатого октября."),
    ("A", "Пятнадцатое октября реально. Нам нужно от вас три вещи: список пользователей, доступ к тестовому серверу и контакт администратора."),
    ("B", "Список пользователей пришлю завтра. Доступ к серверу даст Марина, я её попрошу до пятницы."),
    ("A", "Отлично. По цене: для пилота мы предлагаем тариф Про со скидкой тридцать процентов на первые три месяца."),
    ("B", "Скидка нас устраивает. Но нужен договор с НДС, это обязательное условие нашей бухгалтерии."),
    ("A", "Понял. Договор с НДС я уточню у юристов и вернусь с ответом в понедельник."),
    ("B", "Ещё вопрос: можно ли хранить данные только в Европе? У нас есть требования по безопасности."),
    ("A", "Да, мы разместим машины в Нидерландах. Решено: пилот стартует пятнадцатого октября, серверы в Европе."),
    ("B", "Договорились. Тогда созвонимся в понедельник в одиннадцать утра."),
]

EN = [
    ("A", "Hi Sarah, thanks for joining. Let's do a quick product sync about the mobile release."),
    ("B", "Sure. The main blocker is the login screen. Android crashes when the password field is empty."),
    ("A", "Okay. Can David fix that before Thursday?"),
    ("B", "Yes, David already found the cause. He will ship the fix on Wednesday."),
    ("A", "Great. Next topic: pricing. We decided to keep the monthly plan at ten dollars."),
    ("B", "Agreed. But marketing wants an annual plan too. Should it be ninety six dollars?"),
    ("A", "Let's go with ninety six dollars for the annual plan. I will update the pricing page by Friday."),
    ("B", "One open question: do we support Apple Pay in the first release?"),
    ("A", "Not sure yet. I need to check with the payments team and get back to you next week."),
    ("B", "Perfect. So the release date stays October twentieth."),
    ("A", "Yes, October twentieth. Thanks Sarah, talk soon."),
]

VOICES = {
    "ru": {"A": ("Milena", 175, None), "B": ("Milena", 165, 0.82)},
    "en": {"A": ("Daniel", 180, None), "B": ("Samantha", 180, None)},
}


def run(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speak(tmp, idx, lang, spk, text):
    voice, rate, pitch = VOICES[lang][spk]
    aiff = os.path.join(tmp, f"{idx}.aiff")
    wav = os.path.join(tmp, f"{idx}.wav")
    run("say", "-v", voice, "-r", str(rate), "-o", aiff, text)
    filt = "aresample=16000"
    if pitch:  # pitch down without changing tempo
        filt = f"asetrate=22050*{pitch},atempo={1/pitch:.4f},aresample=16000"
    run("ffmpeg", "-y", "-i", aiff, "-af", filt, "-ac", "1", "-ar", "16000", wav)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", wav], capture_output=True, text=True, check=True)
    return wav, float(out.stdout.strip())


def build(lang, lines, split=False):
    with tempfile.TemporaryDirectory() as tmp:
        silence = os.path.join(tmp, "gap.wav")
        run("ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.7", silence)
        parts, per = [], {"A": [], "B": []}
        for i, (spk, text) in enumerate(lines):
            wav, dur = speak(tmp, i, lang, spk, text)
            parts += [wav, silence]
            # for the split variant: this line on its speaker's track, silence on the other
            mute = os.path.join(tmp, f"{i}-mute.wav")
            run("ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", f"{dur:.3f}", mute)
            other = "B" if spk == "A" else "A"
            per[spk] += [wav, silence]
            per[other] += [mute, silence]

        def concat(files, out):
            lst = os.path.join(tmp, os.path.basename(out) + ".txt")
            with open(lst, "w") as fh:
                fh.writelines(f"file '{f}'\n" for f in files)
            args = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst]
            if out.endswith(".m4a"):
                args += ["-c:a", "aac", "-b:a", "64k"]
            elif out.endswith(".webm"):
                args += ["-c:a", "libopus", "-b:a", "32k"]
            run(*args, out)

        concat(parts, os.path.join(HERE, f"dialog-{lang}.m4a"))
        if split:
            concat(per["A"], os.path.join(HERE, f"dialog-{lang}-mic.webm"))
            concat(per["B"], os.path.join(HERE, f"dialog-{lang}-tab.webm"))


if __name__ == "__main__":
    build("ru", RU)
    build("en", EN, split=True)
    for f in sorted(os.listdir(HERE)):
        if f.startswith("dialog-"):
            print(f)

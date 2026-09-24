"""Присутствие браузера: сервис живёт, пока открыта страница.

Сервис локальный и однопользовательский — держать процесс с загруженными
whisper и pyannote (это гигабайты памяти) после того, как вкладку закрыли,
незачем. Фронт шлёт сердцебиение, здесь оно превращается в решение.

Контракт с фронтом (frontend/app.js):
    POST /api/alive — раз в 5 секунд, пока страница жива;
    POST /api/bye   — маячок на pagehide, «страница ушла».

«Страница ушла» НЕ значит «выключайся сейчас же»: pagehide срабатывает и на
обычной перезагрузке, и при переходе по ссылке внутри приложения. Поэтому
после маячка выжидаем BYE_GRACE_SEC — если вкладка вернулась и снова прислала
сердцебиение, отбой.

Второй путь — молчание. Вкладку могли убить вместе с браузером, и маячок не
ушёл. Тогда срабатывает таймаут по сердцебиению. Он намеренно большой: в
фоновой вкладке браузер душит таймеры до одного раза в минуту, и короткий
порог гасил бы сервер у свёрнутого окна.

Выключение арметcя ТОЛЬКО для локального браузера (127.0.0.1 / ::1). На
боевом хосте посетители приходят с публичных адресов, их сердцебиение
игнорируется, и чужая закрытая вкладка сервер не уронит.
"""

from __future__ import annotations

import os
import signal
import threading
import time

# Сколько ждём после маячка. Перезагрузка страницы укладывается в пару секунд,
# открыть результат в новой вкладке — тоже.
BYE_GRACE_SEC = float(os.environ.get("BYE_GRACE_SEC", "8"))

# Сколько терпим полное молчание. Фоновая вкладка пингует раз в минуту,
# поэтому меньше двух минут ставить нельзя — убьёт свёрнутое окно.
ALIVE_TIMEOUT_SEC = float(os.environ.get("ALIVE_TIMEOUT_SEC", "150"))

# Насколько быстро проверяем. Секунды хватает: спешить некуда.
_TICK_SEC = 1.0

# Сколько ждём вежливого завершения, прежде чем убить процесс силой.
_FORCE_EXIT_AFTER_SEC = 5.0

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on", "да", "так")


# Общий выключатель. Ставь 0, если сервер должен жить сам по себе —
# например под systemd на боевом хосте, куда ходят из локального браузера.
ENABLED = _env_flag("SHUTDOWN_ON_DISCONNECT", True)


def is_local(host: str | None) -> bool:
    return bool(host) and host in _LOOPBACK


class BrowserPresence:
    """Следит за сердцебиением страницы и гасит процесс, когда её не стало."""

    def __init__(
        self,
        enabled: bool = ENABLED,
        bye_grace: float = BYE_GRACE_SEC,
        alive_timeout: float = ALIVE_TIMEOUT_SEC,
    ) -> None:
        self.enabled = enabled
        self.bye_grace = bye_grace
        self.alive_timeout = alive_timeout

        self._lock = threading.Lock()
        # None — сердцебиения ещё не было ни разу. До первого пинга сторож
        # молчит: сервер могли поднять и без браузера (тесты, curl, прогрев),
        # и гасить его в этом случае нельзя.
        self._last_alive: float | None = None
        self._bye_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- сигналы от фронта ---------------------------------------------------

    def touch(self, host: str | None = None) -> bool:
        """Пришло сердцебиение. Возвращает True, если оно принято в расчёт."""
        if not self.enabled or not is_local(host):
            return False
        with self._lock:
            self._last_alive = time.monotonic()
            # Вкладка вернулась — маячок отменяется.
            self._bye_at = None
        return True

    def farewell(self, host: str | None = None) -> bool:
        """Пришёл маячок «страница ушла». Запускает отсчёт, а не выключение."""
        if not self.enabled or not is_local(host):
            return False
        with self._lock:
            if self._last_alive is None:
                # Маячок без единого сердцебиения — не наш браузер.
                return False
            self._bye_at = time.monotonic()
        return True

    # -- сторож --------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._loop, name="browser-presence", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _verdict(self, now: float) -> str | None:
        """Пора ли выключаться и почему. None — рано."""
        with self._lock:
            last_alive, bye_at = self._last_alive, self._bye_at

        if last_alive is None:
            return None
        if bye_at is not None and now - bye_at >= self.bye_grace:
            return "вкладку закрито"
        if now - last_alive > self.alive_timeout:
            return f"сторінка мовчить понад {self.alive_timeout:.0f} с"
        return None

    def _loop(self) -> None:
        while not self._stop.wait(_TICK_SEC):
            reason = self._verdict(time.monotonic())
            if reason:
                self.shutdown(reason)
                return

    # -- выключение ----------------------------------------------------------

    @staticmethod
    def shutdown(reason: str) -> None:
        """Погасить процесс: сначала вежливо, потом наверняка.

        SIGINT — то же самое, что Ctrl+C: uvicorn закрывает соединения и
        отрабатывает lifespan. Если он этого почему-то не сделал (застрял
        в обсчёте, не успел поднять обработчик), через несколько секунд
        добиваем, иначе процесс с моделями останется висеть навсегда.
        """
        print(f"[presence] {reason} — вимикаю сервер", flush=True)

        def _force() -> None:
            time.sleep(_FORCE_EXIT_AFTER_SEC)
            print("[presence] мʼяке завершення не спрацювало — виходжу примусово", flush=True)
            os._exit(0)

        threading.Thread(target=_force, name="force-exit", daemon=True).start()

        try:
            signal.raise_signal(signal.SIGINT)
        except Exception:                                  # noqa: BLE001
            os._exit(0)

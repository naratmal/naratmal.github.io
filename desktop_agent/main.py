"""Desktop agent for monitoring k-Edufine counters.

This application embeds a Qt WebEngine browser that lets a user manually log into
https://klef.goe.go.kr/keris_ui/main.do with their GPKI certificate. After the
user completes the sign-in flow the agent periodically evaluates JavaScript in
the loaded page to read the counters for approval, reference circulation and
other inbox widgets. When any counter changes, the app raises a desktop
notification (if supported) and logs the event inside the UI.

Because the real service is available only inside the administrative network the
selectors that point at each counter element need to be tailored per user. The
values can be edited in the configuration file located next to this script.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

try:  # optional dependency for native notifications
    from plyer import notification
except Exception:  # pragma: no cover - notifications are optional
    notification = None


CONFIG_PATH = Path(__file__).with_name("config").joinpath("selectors.json")
DEFAULT_URL = "https://klef.goe.go.kr/keris_ui/main.do"
CHECK_INTERVAL_MS = 60_000  # 60 seconds


@dataclass
class CounterChange:
    name: str
    old: Optional[str]
    new: Optional[str]
    timestamp: datetime

    def to_display_text(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        return f"[{ts}] {self.name}: {self.old or '-'} → {self.new or '-'}"


class CounterMonitor(QWidget):
    """Main window that owns the embedded browser and monitors counters."""

    def __init__(self, selectors: Dict[str, str]):
        super().__init__()
        self.setWindowTitle("k-Edufine Counter Monitor")
        self.resize(1200, 800)

        self._selectors = selectors
        self._previous_counts: Dict[str, Optional[str]] = {}

        self.browser = QWebEngineView()
        self.browser.load(QUrl(DEFAULT_URL))

        self.status_label = QLabel("로그인 후에 모니터링이 자동으로 시작됩니다.")
        self.log_list = QListWidget()

        self.selector_capture_active = False

        self.selector_button = QPushButton("선택자 추출")
        self.selector_button.clicked.connect(self.capture_selector)

        self.reload_button = QPushButton("페이지 새로고침")
        self.reload_button.clicked.connect(self.browser.reload)

        self.export_button = QPushButton("로그 저장")
        self.export_button.clicked.connect(self.export_log)

        buttons = QHBoxLayout()
        buttons.addWidget(self.selector_button)
        buttons.addWidget(self.reload_button)
        buttons.addWidget(self.export_button)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(self.browser, stretch=3)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("변경 이력"))
        layout.addWidget(self.log_list, stretch=2)

        self.browser.loadStarted.connect(self._cancel_selector_capture)

        self.timer = QTimer(self)
        self.timer.setInterval(CHECK_INTERVAL_MS)
        self.timer.timeout.connect(self.poll_counts)
        self.timer.start()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def export_log(self) -> None:
        """Export the log messages to a UTF-8 text file."""
        if self.log_list.count() == 0:
            self.status_label.setText("저장할 로그가 없습니다.")
            return

        filename, _ = QFileDialog.getSaveFileName(
            self,
            caption="로그 저장",
            dir=str(Path.home()),
            filter="Text files (*.txt)",
        )
        if not filename:
            return

        with open(filename, "w", encoding="utf-8") as f:
            for row in range(self.log_list.count()):
                f.write(self.log_list.item(row).text() + "\n")

        self.status_label.setText(f"로그를 저장했습니다: {filename}")

    def append_change(self, change: CounterChange) -> None:
        self.log_list.insertItem(0, change.to_display_text())
        self.status_label.setText(change.to_display_text())
        if notification is not None:
            try:
                notification.notify(
                    title="k-Edufine 카운트 변경",
                    message=f"{change.name}: {change.old or '-'} → {change.new or '-'}",
                    app_name="k-Edufine Monitor",
                    timeout=5,
                )
            except Exception:
                # Ignore notification issues; UI log is sufficient.
                pass

    # ------------------------------------------------------------------
    # Monitoring logic
    # ------------------------------------------------------------------
    def capture_selector(self) -> None:
        """Enable a visual picker that copies the clicked element's selector."""

        if self.selector_capture_active:
            self.browser.page().runJavaScript(
                "if (window.__cancelSelectorCapture) { window.__cancelSelectorCapture(); }"
            )
            return

        self.selector_capture_active = True
        self.selector_button.setText("선택자 추출 취소")
        self.status_label.setText(
            "선택자 추출 모드입니다. 원하는 숫자를 클릭하면 선택자가 복사됩니다."
        )

        script = self._build_selector_capture_script()
        self.browser.page().runJavaScript(script, self._handle_selector_capture)

    def _cancel_selector_capture(self) -> None:
        if not self.selector_capture_active:
            return

        self.selector_capture_active = False
        self.selector_button.setText("선택자 추출")
        self.status_label.setText("페이지가 새로 로드되었습니다. 다시 시도하세요.")
        self.browser.page().runJavaScript(
            "if (window.__cancelSelectorCapture) { window.__cancelSelectorCapture(); }"
        )

    def _handle_selector_capture(self, result: Optional[str]) -> None:
        self.selector_capture_active = False
        self.selector_button.setText("선택자 추출")

        if not result:
            self.status_label.setText("선택자 정보를 가져오지 못했습니다.")
            return

        if result == "__already_running__":
            self.status_label.setText("이미 선택자 추출 모드가 활성화되어 있습니다.")
            return

        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            self.status_label.setText("선택자 응답을 해석하지 못했습니다.")
            return

        if payload.get("cancelled"):
            self.status_label.setText("선택자 추출을 취소했습니다.")
            return

        selector = payload.get("selector") or ""
        text = payload.get("text") or ""
        QApplication.clipboard().setText(selector)

        message = f"선택자: {selector}"
        if text:
            message += f" (텍스트: {text})"
        self.status_label.setText(message + " — 클립보드에 복사되었습니다.")
        self.log_list.insertItem(0, f"[선택자 추출] {selector} ← {text}")

    def poll_counts(self) -> None:
        """Inject JavaScript into the page to extract counter values."""

        script = self._build_script(self._selectors)
        self.browser.page().runJavaScript(script, self._handle_counts)

    def _handle_counts(self, result: Optional[str]) -> None:
        if not result:
            self.status_label.setText(
                "카운트를 읽지 못했습니다. 로그인 여부와 선택자를 확인하세요."
            )
            return

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            self.status_label.setText("응답 파싱에 실패했습니다.")
            return

        for key, value in data.items():
            previous = self._previous_counts.get(key)
            # Normalize whitespace
            normalized = value.strip() if isinstance(value, str) else None
            if previous != normalized:
                if previous is not None:
                    change = CounterChange(
                        name=key,
                        old=previous,
                        new=normalized,
                        timestamp=datetime.now(),
                    )
                    self.append_change(change)
                self._previous_counts[key] = normalized

    @staticmethod
    def _build_script(selectors: Dict[str, str]) -> str:
        """Create the JavaScript snippet for extracting counter text."""
        parts = ["(function() {", "const result = {};"]
        for name, selector in selectors.items():
            # Escape embedded quotes in selectors
            safe_selector = selector.replace("\\", "\\\\").replace("\"", "\\\"")
            parts.append(
                "result[\"%s\"] = (function() {" % name
            )
            parts.append(
                "  const el = document.querySelector(\"%s\");" % safe_selector
            )
            parts.append("  if (!el) { return null; }")
            parts.append("  const text = el.innerText || el.textContent || '';\n  return text.trim();")
            parts.append("})();")
        parts.append("return JSON.stringify(result);")
        parts.append("})();")
        return "\n".join(parts)

    @staticmethod
    def _build_selector_capture_script() -> str:
        """Return JavaScript that highlights clicks and resolves with a selector."""

        return """
(function() {
  if (window.__selectorCaptureActive) {
    return "__already_running__";
  }
  window.__selectorCaptureActive = true;

  function cssEscape(ident) {
    if (window.CSS && window.CSS.escape) {
      return window.CSS.escape(ident);
    }
    return ident.replace(/([\.\#:\[\]\(\)])/g, '\\$1');
  }

  function buildPath(element) {
    if (!(element instanceof Element)) {
      return "";
    }
    const path = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      let selector = current.tagName.toLowerCase();
      if (current.id) {
        selector = '#' + cssEscape(current.id);
        path.unshift(selector);
        break;
      }
      if (current.classList.length > 0) {
        selector += '.' + Array.from(current.classList).map(cssEscape).join('.');
      }
      let sibling = current;
      let index = 1;
      while (sibling.previousElementSibling) {
        sibling = sibling.previousElementSibling;
        if (sibling.tagName === current.tagName) {
          index += 1;
        }
      }
      if (index > 1) {
        selector += `:nth-of-type(${index})`;
      }
      path.unshift(selector);
      current = current.parentElement;
    }
    return path.join(' > ');
  }

  let resolvePromise;
  const promise = new Promise((resolve) => {
    resolvePromise = resolve;
  });

  const overlay = document.createElement('div');
  overlay.style.position = 'absolute';
  overlay.style.pointerEvents = 'none';
  overlay.style.border = '2px solid #ff4d4f';
  overlay.style.backgroundColor = 'rgba(255, 77, 79, 0.15)';
  overlay.style.zIndex = '2147483647';
  overlay.style.transition = 'all 0.05s ease';

  const label = document.createElement('div');
  label.style.position = 'fixed';
  label.style.pointerEvents = 'none';
  label.style.background = '#ff4d4f';
  label.style.color = '#fff';
  label.style.fontSize = '12px';
  label.style.fontFamily = 'sans-serif';
  label.style.padding = '2px 6px';
  label.style.borderRadius = '4px';
  label.style.zIndex = '2147483647';
  label.style.transform = 'translateY(-100%)';

  const body = document.body;
  if (!body) {
    window.__selectorCaptureActive = false;
    return JSON.stringify({ cancelled: true });
  }
  body.appendChild(overlay);
  body.appendChild(label);
  const previousCursor = body.style.cursor;
  body.style.cursor = 'crosshair';

  function cleanup(payload) {
    window.removeEventListener('mousemove', moveListener, true);
    window.removeEventListener('click', clickListener, true);
    window.removeEventListener('keydown', keyListener, true);
    if (overlay.parentNode) {
      overlay.parentNode.removeChild(overlay);
    }
    if (label.parentNode) {
      label.parentNode.removeChild(label);
    }
    body.style.cursor = previousCursor;
    delete window.__cancelSelectorCapture;
    window.__selectorCaptureActive = false;
    resolvePromise(payload);
  }

  function finish(payload) {
    cleanup(payload);
  }

  function moveListener(event) {
    const target = event.target;
    if (!(target instanceof Element)) {
      overlay.style.display = 'none';
      label.style.display = 'none';
      return;
    }
    const rect = target.getBoundingClientRect();
    overlay.style.display = 'block';
    overlay.style.top = `${window.scrollY + rect.top}px`;
    overlay.style.left = `${window.scrollX + rect.left}px`;
    overlay.style.width = `${rect.width}px`;
    overlay.style.height = `${rect.height}px`;
    label.style.display = 'block';
    label.style.top = `${Math.max(0, rect.top - 6)}px`;
    label.style.left = `${rect.left}px`;
    label.textContent = target.tagName.toLowerCase();
  }

  function clickListener(event) {
    event.preventDefault();
    event.stopPropagation();
    const selector = buildPath(event.target);
    const text = (event.target.innerText || event.target.textContent || '').trim();
    finish({ selector, text });
  }

  function keyListener(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      finish({ cancelled: true });
    }
  }

  window.__cancelSelectorCapture = function() {
    finish({ cancelled: true });
  };

  window.addEventListener('mousemove', moveListener, true);
  window.addEventListener('click', clickListener, true);
  window.addEventListener('keydown', keyListener, true);

  return promise.then((payload) => {
    return JSON.stringify(payload);
  });
})();
        """


def load_selectors(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(
            "카운트 요소의 CSS 선택자를 정의한 selectors.json 파일이 없습니다."
        )
    with open(path, "r", encoding="utf-8") as f:
        selectors = json.load(f)
    if not isinstance(selectors, dict):
        raise ValueError("selectors.json 파일 형식이 잘못되었습니다. 객체 형태여야 합니다.")
    return selectors


def main() -> int:
    try:
        selectors = load_selectors(CONFIG_PATH)
    except Exception as exc:  # pragma: no cover - setup errors surface in UI
        print(f"설정 파일을 불러오지 못했습니다: {exc}")
        return 1

    app = QApplication(sys.argv)
    monitor = CounterMonitor(selectors)
    monitor.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

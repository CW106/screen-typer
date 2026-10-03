# ScreenTyper — OCR Auto Typing

[繁體中文說明](README.md)

Select part of your screen, recognize its text with Windows OCR, and type it into the active window using simulated keyboard input. You can control speed, accuracy, timing variation, and automatic monitoring. The English launcher translates the app interface; OCR still supports whichever languages are installed in Windows.

## Install and launch

Requires Windows 10 or 11, Python, Pillow, and Windows OCR support for the language you want to recognize. Windows `SendInput` handles typing.

```powershell
python -m pip install -r requirements.txt
python -m screen_typer --english
```

You can also double-click `Launch ScreenTyper (English).bat`. Double-click `啟動.bat` or run `python -m screen_typer` for the Chinese interface.

## Three steps

| Step | Button / hotkey | Action |
| --- | --- | --- |
| 1 | **Select area** / F9 | Drag around the text to recognize. Works across monitors; Esc cancels. |
| 2 | **Recognize** / F10 | Put the OCR result in the editable text box. |
| 3 | **Start typing** / F11 | Wait for the countdown, switch to the target window, and start typing. |
| Stop | **Stop** / Esc | Stop at any time, even when another window is active. |

Gray text in the editor is considered already typed and is skipped. Black text is what the app will type.

## Scrolling typing tests

Some typing tests scroll as you type and highlight the next character. Include the highlighted cursor line and several lines below it when selecting the area. The defaults **Start at highlighted cursor**, **Skip the last incomplete word**, and **Join lines with spaces** are intended for these tests. Joining lines matters because visual line breaks in a continuous passage usually should not become Enter keystrokes.

Enable **Auto-watch** to rescan at the chosen interval and type newly detected text. The app waits for two matching OCR reads before treating the screen as stable.

## Controls

| Control | Meaning |
| --- | --- |
| **Speed WPM** | Target average speed including pauses, using five characters per word. |
| **Accuracy %** | Share of final characters left correct; deliberate mistakes are left uncorrected. Set 100% to disable those mistakes. |
| **Rhythm variation** | How much the gap between keystrokes varies. |
| **Chance of correcting a typo** | Sometimes type a wrong key, pause, backspace, then correct it. This does not reduce final accuracy. |
| **Start delay** | Time to focus the target window after pressing Start. |
| **Scale / High contrast** | Help Windows OCR read small or faint text. |
| **Restore highlighted text** | Normalize highlighted cursor text before OCR. |

Typing speed is limited by the time needed to send keystrokes; the app warns when a requested WPM exceeds that limit. The rhythm model includes varying intervals, punctuation pauses, and occasional longer pauses.

## Limitations

- Windows OCR can misread text. Increase Scale or try High contrast, then edit the recognized text before pressing F11.
- Highlight restoration can sometimes misidentify a light card on a dark background. Turn **Restore highlighted text** off if the result looks worse.
- The language menu lists only installed Windows OCR languages. Add one through **Settings → Time & language → Language & region → Language options → Optical character recognition**.
- Typing goes to the foreground window, so focus the correct field during the countdown. Some full-screen games that use DirectInput may ignore `SendInput`.

## Files and privacy

The `screen_typer/` package contains the interface, OCR, region selection, highlight processing, and keyboard simulation. Runtime captures are stored in `captures/` (the latest 20 images) on your own computer. `captures/` and the original development test samples are excluded from this public repository. Preferences are saved to `~/.screen_typer.json`.

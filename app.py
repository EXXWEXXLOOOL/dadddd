from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

APP_NAME = "GTA V Add-On Installer"
DLC_RELATIVE = Path("mods") / "update" / "x64" / "dlcpacks"
DLCLIST_ENTRY = "dlcpacks:/{}/"


def safe_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value)
    value = value.strip("_")
    if not value:
        raise ValueError("Не удалось определить допустимое имя DLC.")
    return value


def find_dlc_roots(folder: Path) -> list[Path]:
    """Find every directory that directly contains a file named dlc.rpf."""
    candidates = sorted(
        {path.parent for path in folder.rglob("dlc.rpf") if path.is_file()},
        key=lambda path: str(path).lower(),
    )
    if not candidates:
        raise ValueError("В выбранном источнике не найден ни один файл dlc.rpf.")
    return candidates


def validate_dlclist(xml_path: Path) -> str:
    if not xml_path.is_file():
        raise FileNotFoundError(f"Файл не найден: {xml_path}")
    content = xml_path.read_text(encoding="utf-8-sig")
    try:
        ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"Некорректный XML: {exc}") from exc
    if not re.search(r"</Paths\s*>", content, re.IGNORECASE):
        raise ValueError("В XML не найден закрывающий тег </Paths>.")
    return content


def update_dlclist(xml_path: Path, pack_names: list[str]) -> tuple[list[str], Path | None]:
    content = validate_dlclist(xml_path)
    missing: list[str] = []
    for pack_name in pack_names:
        entry = DLCLIST_ENTRY.format(pack_name)
        pattern = re.compile(
            r"<Item>\s*" + re.escape(entry) + r"\s*</Item>",
            re.IGNORECASE,
        )
        if not pattern.search(content):
            missing.append(pack_name)

    if not missing:
        return [], None

    backup = xml_path.with_name(
        f"{xml_path.name}.backup-{datetime.now():%Y%m%d-%H%M%S-%f}"
    )
    shutil.copy2(xml_path, backup)

    updated = content
    for pack_name in missing:
        entry = DLCLIST_ENTRY.format(pack_name)
        updated = re.sub(
            r"(</Paths\s*>)",
            f"    <Item>{entry}</Item>\n\\1",
            updated,
            count=1,
            flags=re.IGNORECASE,
        )

    # Validate the full XML before replacing the original file.
    ET.fromstring(updated)
    temp_path = xml_path.with_name(f".{xml_path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(updated, encoding="utf-8")
        os.replace(temp_path, xml_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return missing, backup


def copy_dlc_contents(source_root: Path, destination: Path) -> None:
    if not (source_root / "dlc.rpf").is_file():
        raise ValueError("В выбранной папке не найден dlc.rpf.")
    destination.mkdir(parents=True, exist_ok=False)
    for item in source_root.iterdir():
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            normalized = member.filename.replace("\\", "/")
            parts = Path(normalized).parts
            if (
                not normalized
                or normalized.startswith("/")
                or any(part in ("..", "") for part in parts)
                or (parts and ":" in parts[0])
            ):
                raise ValueError("ZIP-архив содержит небезопасный путь.")
            target = destination.joinpath(*parts)
            resolved = target.resolve()
            if destination.resolve() not in resolved.parents and resolved != destination.resolve():
                raise ValueError("Архив содержит небезопасный путь.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def find_rar_extractor() -> tuple[str, str] | None:
    """Return (executable, kind); RAR needs 7-Zip or WinRAR/UnRAR installed."""
    for name in ("7z", "7zz", "7za"):
        executable = shutil.which(name)
        if executable:
            return executable, "7zip"

    for name in ("UnRAR", "unrar"):
        executable = shutil.which(name)
        if executable:
            return executable, "unrar"

    program_dirs = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ]
    for base in filter(None, program_dirs):
        for relative in (
            Path("7-Zip") / "7z.exe",
            Path("WinRAR") / "UnRAR.exe",
            Path("WinRAR") / "UnRAR",
        ):
            candidate = Path(base) / relative
            if candidate.is_file():
                return str(candidate), "7zip" if "7-Zip" in str(relative) else "unrar"
    return None


def extract_rar(archive: Path, destination: Path) -> None:
    extractor = find_rar_extractor()
    if extractor is None:
        raise RuntimeError(
            "Для распаковки RAR установите 7-Zip или WinRAR. "
            "После установки перезапустите программу."
        )
    executable, kind = extractor
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "7zip":
        command = [executable, "x", "-y", f"-o{destination}", str(archive)]
    else:
        command = [executable, "x", "-o+", str(archive), str(destination) + os.sep]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=900,
    )
    if result.returncode != 0:
        details = (result.stdout or "") + "\n" + (result.stderr or "")
        raise RuntimeError("Не удалось распаковать RAR.\n" + details[-1800:])


def prepare_sources(source: Path, extraction_dir: Path) -> list[tuple[Path, str]]:
    """Extract source if needed and return every (DLC folder, install name) pair."""
    if source.is_dir():
        scan_root = source
        naming_base = source
    elif source.suffix.lower() == ".zip":
        extract_zip(source, extraction_dir)
        scan_root = extraction_dir
        naming_base = extraction_dir
    elif source.suffix.lower() == ".rar":
        extract_rar(source, extraction_dir)
        scan_root = extraction_dir
        naming_base = extraction_dir
    else:
        raise ValueError("Поддерживаются ZIP, RAR и папки с DLC.")

    dlc_roots = find_dlc_roots(scan_root)
    packs: list[tuple[Path, str]] = []
    used_names: dict[str, Path] = {}

    for dlc_root in dlc_roots:
        relative = dlc_root.relative_to(naming_base)
        raw_name = relative.name if relative.parts else source.stem if not source.is_dir() else source.name
        pack_name = safe_name(raw_name)
        if pack_name.lower() in used_names:
            other = used_names[pack_name.lower()]
            raise ValueError(
                "У нескольких DLC получилось одинаковое имя «"
                f"{pack_name}»:\n{other}\n{dlc_root}\n"
                "Переименуйте папки DLC в архиве так, чтобы их имена различались."
            )
        used_names[pack_name.lower()] = dlc_root
        packs.append((dlc_root, pack_name))

    return packs


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_NAME)
        root.geometry("720x510")
        root.minsize(620, 460)

        self.source_var = tk.StringVar()
        self.game_var = tk.StringVar()
        self.xml_var = tk.StringVar()
        self.name_var = tk.StringVar()

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="GTA V Add-On Installer", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Перетащи один ZIP/RAR с одной или несколькими машинами: программа найдёт все папки с dlc.rpf и установит их пакетно.",
            wraplength=670,
        ).pack(anchor="w", pady=(4, 18))

        self._path_row(frame, "Архив ZIP/RAR или папка DLC", self.source_var, self.pick_source)
        self._path_row(frame, "Папка GTA V", self.game_var, self.pick_game)
        self._path_row(frame, "dlclist.xml (извлечённый через OpenIV)", self.xml_var, self.pick_xml)

        name_row = ttk.Frame(frame)
        name_row.pack(fill="x", pady=7)
        ttk.Label(name_row, text="Имя DLC (для одной машины):", width=29).pack(side="left")
        ttk.Entry(name_row, textvariable=self.name_var).pack(side="left", fill="x", expand=True)
        ttk.Label(
            frame,
            text="Оставь поле пустым для автоимён из папок с dlc.rpf. Для архива с несколькими машинами поле должно быть пустым.",
            foreground="#666",
            wraplength=670,
        ).pack(anchor="w", pady=(0, 12))

        self.install_button = ttk.Button(frame, text="УСТАНОВИТЬ DLC", command=self.start_install)
        self.install_button.pack(fill="x", ipady=8, pady=(4, 12))

        ttk.Label(
            frame,
            text="Можно перетащить ZIP/RAR или папку DLC прямо в это окно.",
            foreground="#666",
        ).pack(anchor="w", pady=(0, 8))

        self.status = tk.StringVar(value="Готово к установке.")
        ttk.Label(frame, textvariable=self.status, wraplength=670).pack(anchor="w")
        if DND_FILES is not None:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self.on_drop)
        ttk.Label(
            frame,
            text="Перед установкой закройте GTA V. Программа не редактирует update.rpf напрямую.",
            foreground="#9a5b00",
            wraplength=670,
        ).pack(anchor="w", pady=(14, 0))

    def _path_row(self, parent, label, variable, command):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=7)
        ttk.Label(row, text=label, width=29).pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор…", command=command).pack(side="left", padx=(7, 0))

    def pick_source(self):
        choice = messagebox.askyesnocancel(
            "Выбор источника",
            "Да — выбрать ZIP или RAR.\nНет — выбрать папку с DLC.",
        )
        if choice is None:
            return
        if choice:
            path = filedialog.askopenfilename(filetypes=[("Архивы ZIP/RAR", "*.zip *.rar"), ("ZIP", "*.zip"), ("RAR", "*.rar"), ("Все файлы", "*.*")])
        else:
            path = filedialog.askdirectory()
        if path:
            self.source_var.set(path)
            # Empty name means auto-detect from the folder containing dlc.rpf.

    def on_drop(self, event):
        try:
            dropped = self.root.tk.splitlist(event.data)
            if len(dropped) != 1:
                raise ValueError("Перетащите один ZIP/RAR-архив или одну папку DLC.")
            path = Path(dropped[0])
            if not path.exists():
                raise FileNotFoundError("Перетащенный файл или папка не найдены.")
            if not path.is_dir() and path.suffix.lower() not in (".zip", ".rar"):
                raise ValueError("Поддерживаются ZIP, RAR или папки с dlc.rpf.")
            self.source_var.set(str(path))
            self.status.set(f"Источник выбран: {path.name}")
        except Exception as exc:
            messagebox.showerror("Не удалось принять файл", str(exc))

    def pick_game(self):
        path = filedialog.askdirectory(title="Выберите корневую папку GTA V")
        if path:
            self.game_var.set(path)

    def pick_xml(self):
        path = filedialog.askopenfilename(
            title="Выберите извлечённый dlclist.xml",
            filetypes=[("XML файлы", "*.xml"), ("Все файлы", "*.*")],
        )
        if path:
            self.xml_var.set(path)

    def start_install(self):
        source_text = self.source_var.get().strip()
        game_text = self.game_var.get().strip()
        xml_text = self.xml_var.get().strip()
        if not source_text or not game_text or not xml_text:
            messagebox.showwarning(
                "Не все данные указаны",
                "Выберите архив/папку с DLC, папку GTA V и извлечённый dlclist.xml.",
            )
            return

        source = Path(source_text)
        game = Path(game_text)
        xml_path = Path(xml_text)
        if not source.exists() or not game.is_dir():
            messagebox.showerror("Ошибка", "Проверьте выбранные пути.")
            return

        try:
            manual_name = safe_name(self.name_var.get()) if self.name_var.get().strip() else None
            validate_dlclist(xml_path)
        except Exception as exc:
            messagebox.showerror("Проверка не пройдена", str(exc))
            return

        self.install_button.configure(state="disabled")
        self.status.set("Анализ архива и установка…")
        threading.Thread(
            target=self._install_worker,
            args=(source, game, xml_path, manual_name),
            daemon=True,
        ).start()

    def _install_worker(self, source, game, xml_path, manual_name):
        published: list[Path] = []
        staging_dirs: list[Path] = []
        try:
            dlcpacks = game / DLC_RELATIVE
            dlcpacks.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(prefix="gtav_addon_") as temp_dir:
                extraction_dir = Path(temp_dir) / "extracted"
                extraction_dir.mkdir()
                packs = prepare_sources(source, extraction_dir)

                if manual_name and len(packs) != 1:
                    raise ValueError(
                        "Поле «Имя DLC» можно заполнять только при установке одной машины. "
                        "Для архива с несколькими машинами очистите это поле."
                    )
                if manual_name:
                    packs = [(packs[0][0], manual_name)]

                pack_names = [name for _, name in packs]
                destinations = [dlcpacks / name for name in pack_names]

                # Preflight the complete batch before copying anything.
                conflicts = [path for path in destinations if path.exists()]
                if conflicts:
                    conflict_text = "\n".join(str(path) for path in conflicts)
                    raise FileExistsError(
                        "Установка отменена: следующие папки DLC уже существуют:\n"
                        f"{conflict_text}\n\n"
                        "Ничего не перезаписано. Удалите старые папки вручную "
                        "или переименуйте папки DLC в архиве."
                    )

                # Copy every DLC to a temporary sibling folder first.
                for (source_root, pack_name), destination in zip(packs, destinations):
                    staging = Path(tempfile.mkdtemp(
                        prefix=f".{pack_name}.installing-",
                        dir=dlcpacks,
                    ))
                    staging_dirs.append(staging)
                    for item in source_root.iterdir():
                        target = staging / item.name
                        if item.is_dir():
                            shutil.copytree(item, target)
                        else:
                            shutil.copy2(item, target)
                    if not (staging / "dlc.rpf").is_file():
                        raise ValueError(
                            f"В папке «{pack_name}» после копирования не найден dlc.rpf."
                        )

                # Publish only after all DLCs have been staged successfully.
                for staging, destination in zip(staging_dirs, destinations):
                    if destination.exists():
                        raise FileExistsError(
                            f"Папка DLC появилась во время установки: {destination}"
                        )
                    staging.rename(destination)
                    published.append(destination)
                staging_dirs.clear()

            try:
                added_names, backup = update_dlclist(xml_path, pack_names)
            except Exception:
                for destination in published:
                    shutil.rmtree(destination, ignore_errors=True)
                published.clear()
                raise

            already_registered = [name for name in pack_names if name not in added_names]
            lines = [
                "Пакетная установка завершена!",
                "",
                f"Установлено DLC: {len(pack_names)}",
                *[f"• {name}" for name in pack_names],
                "",
                f"Новых записей в dlclist.xml: {len(added_names)}",
            ]
            if already_registered:
                lines.append("Уже были в dlclist.xml: " + ", ".join(already_registered))
            lines.append(f"Резервная копия XML: {backup if backup else 'не требовалась'}")
            lines.append(f"Папка установки: {dlcpacks}")
            result = "\n".join(lines)
            self.root.after(0, lambda result=result: self._finish(True, result))
        except Exception as exc:
            for staging in staging_dirs:
                shutil.rmtree(staging, ignore_errors=True)
            # Roll back any folders already published if the batch did not finish.
            for destination in published:
                shutil.rmtree(destination, ignore_errors=True)
            published.clear()
            error = str(exc)
            self.root.after(0, lambda error=error: self._finish(False, error))

    def _finish(self, success, message):
        self.install_button.configure(state="normal")
        self.status.set("Готово." if success else "Ошибка установки.")
        if success:
            messagebox.showinfo("Установка завершена", message)
        else:
            messagebox.showerror("Ошибка установки", message)


def main():
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    InstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

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


def find_dlc_root(folder: Path) -> Path:
    """Return the directory that directly contains dlc.rpf."""
    candidates = [p.parent for p in folder.rglob("dlc.rpf") if p.is_file()]
    if not candidates:
        raise ValueError("В выбранной папке не найден dlc.rpf.")
    if len(candidates) > 1:
        # Prefer a dlc.rpf directly in the selected folder; otherwise ask user
        direct = folder / "dlc.rpf"
        if direct.is_file():
            return folder
        raise ValueError(
            "Найдено несколько файлов dlc.rpf. Распакуйте нужный DLC отдельно "
            "и выберите папку, в которой лежит ровно один dlc.rpf."
        )
    return candidates[0]


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


def update_dlclist(xml_path: Path, pack_name: str) -> tuple[bool, Path | None]:
    content = validate_dlclist(xml_path)
    entry = DLCLIST_ENTRY.format(pack_name)
    # Match the complete DLC entry regardless of whitespace around it.
    pattern = re.compile(
        r"<Item>\s*" + re.escape(entry) + r"\s*</Item>",
        re.IGNORECASE,
    )
    if pattern.search(content):
        return False, None

    backup = xml_path.with_name(
        f"{xml_path.name}.backup-{datetime.now():%Y%m%d-%H%M%S}"
    )
    shutil.copy2(xml_path, backup)

    # Keep the existing XML formatting and insert one item before </Paths>.
    updated = re.sub(
        r"(</Paths\s*>)",
        f"    <Item>{entry}</Item>\n\\1",
        content,
        count=1,
        flags=re.IGNORECASE,
    )
    # Validate before replacing the user's file.
    ET.fromstring(updated)
    temp_path = xml_path.with_suffix(xml_path.suffix + ".tmp")
    temp_path.write_text(updated, encoding="utf-8")
    os.replace(temp_path, xml_path)
    return True, backup


def install_from_folder(source: Path, destination: Path) -> None:
    source_root = find_dlc_root(source)
    destination.mkdir(parents=True, exist_ok=True)
    for item in source_root.iterdir():
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def install_from_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        dlc_members = [
            m for m in members
            if Path(m.filename.replace("\\", "/")).name.lower() == "dlc.rpf"
        ]
        if not dlc_members:
            raise ValueError("В ZIP-архиве не найден dlc.rpf.")
        if len(dlc_members) > 1:
            raise ValueError(
                "В архиве несколько dlc.rpf. Используйте архив только с одним DLC."
            )

        dlc_member = dlc_members[0]
        parts = Path(dlc_member.filename.replace("\\", "/")).parts
        inner_prefix = "/".join(parts[:-1])
        # Extract the directory containing dlc.rpf, not unrelated archive files.
        for member in zf.infolist():
            normalized = member.filename.replace("\\", "/").rstrip("/")
            if not normalized:
                continue
            if inner_prefix:
                if normalized != inner_prefix and not normalized.startswith(inner_prefix + "/"):
                    continue
                relative = normalized[len(inner_prefix):].lstrip("/")
            else:
                relative = normalized
            if not relative:
                continue
            target = destination.joinpath(*Path(relative).parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # Prevent ZIP path traversal.
            resolved = target.resolve()
            if destination.resolve() not in resolved.parents and resolved != destination.resolve():
                raise ValueError("Архив содержит небезопасный путь.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    if not (destination / "dlc.rpf").is_file():
        raise ValueError("Не удалось извлечь dlc.rpf из архива.")


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
            text="Установка DLC-машин в mods/update/x64/dlcpacks и регистрация в dlclist.xml",
            wraplength=670,
        ).pack(anchor="w", pady=(4, 18))

        self._path_row(frame, "Архив ZIP или папка с dlc.rpf", self.source_var, self.pick_source)
        self._path_row(frame, "Папка GTA V", self.game_var, self.pick_game)
        self._path_row(frame, "Извлечённый dlclist.xml (из update.rpf)", self.xml_var, self.pick_xml)

        name_row = ttk.Frame(frame)
        name_row.pack(fill="x", pady=7)
        ttk.Label(name_row, text="Имя DLC:", width=29).pack(side="left")
        ttk.Entry(name_row, textvariable=self.name_var).pack(side="left", fill="x", expand=True)
        ttk.Label(
            frame,
            text="Имя DLC можно оставить пустым: оно будет взято из имени архива или папки.",
            foreground="#666",
            wraplength=670,
        ).pack(anchor="w", pady=(0, 12))

        self.install_button = ttk.Button(frame, text="УСТАНОВИТЬ МАШИНУ", command=self.start_install)
        self.install_button.pack(fill="x", ipady=8, pady=(4, 12))

        self.status = tk.StringVar(value="Готово к установке.")
        ttk.Label(frame, textvariable=self.status, wraplength=670).pack(anchor="w")
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
            "Да — выбрать ZIP-архив.\nНет — выбрать папку с DLC.",
        )
        if choice is None:
            return
        if choice:
            path = filedialog.askopenfilename(filetypes=[("ZIP архивы", "*.zip"), ("Все файлы", "*.*")])
        else:
            path = filedialog.askdirectory()
        if path:
            self.source_var.set(path)
            if not self.name_var.get().strip():
                self.name_var.set(Path(path).stem)

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
            messagebox.showwarning("Не все данные указаны", "Выберите источник DLC, папку GTA V и dlclist.xml.")
            return

        source = Path(source_text)
        game = Path(game_text)
        xml_path = Path(xml_text)
        if not source.exists() or not game.is_dir():
            messagebox.showerror("Ошибка", "Проверьте выбранные пути.")
            return

        try:
            pack_name = safe_name(self.name_var.get() or source.stem)
            validate_dlclist(xml_path)
        except Exception as exc:
            messagebox.showerror("Проверка не пройдена", str(exc))
            return

        destination = game / DLC_RELATIVE / pack_name
        if destination.exists():
            if not messagebox.askyesno(
                "DLC уже существует",
                f"Папка {destination} уже существует. Перезаписать её содержимое?",
            ):
                return

        self.install_button.configure(state="disabled")
        self.status.set("Установка…")
        threading.Thread(
            target=self._install_worker,
            args=(source, destination, xml_path, pack_name),
            daemon=True,
        ).start()

    def _install_worker(self, source, destination, xml_path, pack_name):
        try:
            if source.is_dir():
                install_from_folder(source, destination)
            elif source.suffix.lower() == ".zip":
                install_from_zip(source, destination)
            else:
                raise ValueError("Поддерживаются ZIP-архивы и папки с dlc.rpf.")

            added, backup = update_dlclist(xml_path, pack_name)
            result = (
                f"Установка завершена!\n\nDLC: {pack_name}\n"
                f"Папка: {destination}\n"
                f"Запись в dlclist.xml: {'добавлена' if added else 'уже существовала'}\n"
                f"Резервная копия: {backup if backup else 'не требовалась'}"
            )
            self.root.after(0, lambda: self._finish(True, result))
        except Exception as exc:
            self.root.after(0, lambda: self._finish(False, str(exc)))

    def _finish(self, success, message):
        self.install_button.configure(state="normal")
        self.status.set("Готово." if success else "Ошибка установки.")
        if success:
            messagebox.showinfo("Установка завершена", message)
        else:
            messagebox.showerror("Ошибка установки", message)


def main():
    root = tk.Tk()
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

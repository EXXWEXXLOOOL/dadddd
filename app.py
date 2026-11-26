from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path, PureWindowsPath
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

APP_NAME = "GTA V Add-On Installer"
DLCLIST_ENTRY = "dlcpacks:/{}/"
MODS_RPF_RELATIVE = Path("mods") / "update" / "update.rpf"


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


def validate_game_edition(game_root: Path) -> str:
    """This OpenIV OIV workflow is currently limited to GTA V Legacy."""
    if (game_root / "GTA5_Enhanced.exe").is_file():
        raise RuntimeError(
            "Обнаружена GTA V Enhanced. Эта версия установщика использует "
            "OIV-установщик OpenIV для Legacy и пока не редактирует RPF Enhanced. "
            "Ничего не установлено и файлы игры не изменены."
        )
    if (game_root / "GTA5.exe").is_file():
        return "Legacy"
    raise FileNotFoundError(
        "В выбранной папке не найден GTA5.exe. Выбери корневую папку GTA V Legacy."
    )


def ensure_inside_mods(path: Path, mods_root: Path) -> Path:
    """Resolve and reject any target that escapes the game's mods directory."""
    mods_resolved = mods_root.resolve()
    target_resolved = path.resolve()
    try:
        target_resolved.relative_to(mods_resolved)
    except ValueError as exc:
        raise PermissionError(
            f"Запрещена запись вне папки mods: {target_resolved}"
        ) from exc
    return target_resolved


def ensure_mods_root(game_root: Path) -> Path:
    """Create the real mods directory, rejecting links and redirects."""
    mods_root = game_root / "mods"
    if mods_root.is_symlink():
        raise PermissionError(
            "Папка mods является символической ссылкой. Запись запрещена."
        )
    if not mods_root.exists():
        mods_root.mkdir(parents=False, exist_ok=False)
    if not mods_root.is_dir() or mods_root.resolve() != mods_root:
        raise PermissionError(
            "Папка mods является ссылкой, перенаправлением или не является папкой. "
            "Запись запрещена."
        )
    return mods_root


def ensure_mods_update_rpf(game_root: Path) -> tuple[Path, bool]:
    """Make sure mods/update/update.rpf exists; copy the original only if needed."""
    game_root = game_root.resolve()
    mods_root = ensure_mods_root(game_root)

    update_dir = mods_root / "update"
    ensure_inside_mods(update_dir, mods_root)
    update_dir.mkdir(parents=True, exist_ok=True)

    target = game_root / MODS_RPF_RELATIVE
    ensure_inside_mods(target, mods_root)

    if target.exists():
        if not target.is_file():
            raise ValueError(f"Вместо update.rpf найден не файл: {target}")
        return target, False

    original = game_root / "update" / "update.rpf"
    if not original.is_file():
        raise FileNotFoundError(
            "Не найден mods\\update\\update.rpf и не удалось найти исходный "
            "update\\update.rpf для создания копии."
        )

    temporary_copy = target.with_name(f".{target.name}.{uuid.uuid4().hex}.copying")
    try:
        shutil.copy2(original, temporary_copy)
        if temporary_copy.stat().st_size != original.stat().st_size:
            raise OSError("Копия update.rpf получилась неполной.")
        os.replace(temporary_copy, target)
    finally:
        if temporary_copy.exists():
            temporary_copy.unlink()

    ensure_inside_mods(target, mods_root)
    return target, True


def backup_mods_update_rpf(archive_path: Path, mods_root: Path) -> Path:
    """Create a timestamped backup beside update.rpf, inside mods only."""
    ensure_inside_mods(archive_path, mods_root)
    backup = archive_path.with_name(
        f"{archive_path.name}.backup-{datetime.now():%Y%m%d-%H%M%S-%f}"
    )
    ensure_inside_mods(backup, mods_root)
    shutil.copy2(archive_path, backup)
    if backup.stat().st_size != archive_path.stat().st_size:
        backup.unlink(missing_ok=True)
        raise OSError("Не удалось проверить резервную копию update.rpf.")
    return backup


def build_openiv_package(
    packs: list[tuple[Path, str]],
    package_path: Path,
) -> Path:
    """Build an OIV installer whose every game target is explicitly under mods\\."""
    package_path.parent.mkdir(parents=True, exist_ok=True)

    package = ET.Element(
        "package",
        {
            "version": "2.2",
            "id": "{" + str(uuid.uuid4()).upper() + "}",
            "target": "Five",
        },
    )

    metadata = ET.SubElement(package, "metadata")
    ET.SubElement(metadata, "name").text = "GTA V Add-On Installer"
    version = ET.SubElement(metadata, "version")
    ET.SubElement(version, "major").text = "1"
    ET.SubElement(version, "minor").text = "0"
    author = ET.SubElement(metadata, "author")
    ET.SubElement(author, "displayName").text = "GTA V Add-On Installer"
    description_text = (
        "Installs add-on DLC packs and registers them in "
        "mods\\update\\update.rpf only. No original game archive is a target."
    )
    ET.SubElement(metadata, "description").text = description_text

    colors = ET.SubElement(package, "colors")
    ET.SubElement(
        colors,
        "headerBackground",
        {"useBlackTextColor": "False"},
    ).text = "$FF202A44"
    ET.SubElement(colors, "iconBackground").text = "$FF3B5998"

    content = ET.SubElement(package, "content")
    mods_file_root = PureWindowsPath(
        "mods", "update", "x64", "dlcpacks"
    )

    # Add every file from every DLC folder, always to an explicit mods\\ destination.
    for source_root, pack_name in packs:
        for source_file in sorted(source_root.rglob("*")):
            if source_file.is_symlink():
                raise ValueError(
                    f"В DLC обнаружена символическая ссылка: {source_file}"
                )
            if not source_file.is_file():
                continue

            relative = source_file.relative_to(source_root)
            package_source = PureWindowsPath(pack_name, *relative.parts)
            game_target = mods_file_root / pack_name
            for part in relative.parts:
                game_target = game_target / part

            target_text = str(game_target)
            if not target_text.lower().startswith("mods\\"):
                raise PermissionError(
                    f"Внутренняя проверка запретила путь вне mods: {target_text}"
                )

            add_node = ET.SubElement(
                content,
                "add",
                {"source": str(package_source)},
            )
            add_node.text = target_text

    # OpenIV applies this XPath edit inside this exact archive path, not update\\update.rpf.
    archive_path = r"mods\update\update.rpf"
    if not archive_path.lower().startswith("mods\\"):
        raise PermissionError("Запрещён путь к RPF вне папки mods.")

    archive_node = ET.SubElement(
        content,
        "archive",
        {
            "path": archive_path,
            "createIfNotExist": "False",
            "type": "RPF7",
        },
    )
    xml_node = ET.SubElement(
        archive_node,
        "xml",
        {"path": r"common\data\dlclist.xml"},
    )

    for _, pack_name in packs:
        entry = DLCLIST_ENTRY.format(pack_name)
        xpath = (
            "/SMandatoryPacksData/Paths"
            f"[not(Item[text()='{entry}'])]"
        )
        add_node = ET.SubElement(
            xml_node,
            "add",
            {"xpath": xpath, "append": "Last"},
        )
        ET.SubElement(add_node, "Item").text = entry

    # OIV 2.2 documents description as CDATA; ElementTree doesn't emit CDATA itself.
    assembly_text = ET.tostring(
        package,
        encoding="unicode",
        xml_declaration=False,
    )
    escaped_description = (
        description_text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    assembly_text = assembly_text.replace(
        f"<description>{escaped_description}</description>",
        f"<description><![CDATA[{description_text}]]></description>",
    )
    assembly = (
        '<?xml version="1.0" encoding="utf-8"?>\n' + assembly_text
    ).encode("utf-8")

    with zipfile.ZipFile(
        package_path,
        "w",
        # DLC RPF files are already packed; storing them avoids a slow second compression pass.
        compression=zipfile.ZIP_STORED,
    ) as package_zip:
        package_zip.writestr("assembly.xml", assembly)
        package_zip.writestr(
            "content/README.txt",
            "Generated by GTA V Add-On Installer. "
            "All game targets in this OIV package are under the mods folder.\n",
        )
        for source_root, pack_name in packs:
            for source_file in sorted(source_root.rglob("*")):
                if not source_file.is_file():
                    continue
                relative = source_file.relative_to(source_root)
                package_member = (
                    PureWindowsPath("content", pack_name, *relative.parts)
                    .as_posix()
                )
                package_zip.write(source_file, package_member)

    # Verify the generated package is readable and the manifest contains only mods destinations.
    with zipfile.ZipFile(package_path, "r") as package_zip:
        manifest = ET.fromstring(package_zip.read("assembly.xml"))
        for node in manifest.findall("./content/add"):
            target = (node.text or "").replace("/", "\\").lower()
            if not target.startswith("mods\\"):
                raise PermissionError(
                    f"OIV-пакет содержит запрещённый путь: {node.text}"
                )
        for node in manifest.findall(".//archive"):
            target = node.attrib.get("path", "").replace("/", "\\").lower()
            if not target.startswith("mods\\"):
                raise PermissionError(
                    f"OIV-пакет содержит архив вне mods: {node.attrib.get('path')}"
                )
            for xml_target in node.findall("./xml"):
                xml_path = xml_target.attrib.get("path", "").replace("/", "\\").lower()
                if xml_path != "common\\data\\dlclist.xml":
                    raise PermissionError(
                        f"OIV-пакет содержит неожиданную XML-цель: {xml_path}"
                    )

    return package_path


def launch_openiv_package(package_path: Path) -> bool:
    """Open the generated OIV package through Windows file association."""
    if os.name != "nt":
        return False
    try:
        os.startfile(str(package_path))  # type: ignore[attr-defined]
        return True
    except OSError:
        return False




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
        root.geometry("720x440")
        root.minsize(620, 420)

        self.source_var = tk.StringVar()
        self.game_var = tk.StringVar()
        self.name_var = tk.StringVar()

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="GTA V Add-On Installer", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Перетащи ZIP/RAR с машинами. Программа подготовит установочный пакет OpenIV, который запишет DLC и dlclist.xml только в mods.",
            wraplength=670,
        ).pack(anchor="w", pady=(4, 18))

        self._path_row(frame, "Архив ZIP/RAR или папка DLC", self.source_var, self.pick_source)
        self._path_row(frame, "Папка GTA V", self.game_var, self.pick_game)

        name_row = ttk.Frame(frame)
        name_row.pack(fill="x", pady=7)
        ttk.Label(name_row, text="Имя DLC (для одной машины):", width=29).pack(side="left")
        ttk.Entry(name_row, textvariable=self.name_var).pack(side="left", fill="x", expand=True)
        ttk.Label(
            frame,
            text="Оставь поле пустым для автоимён. Для архива с несколькими машинами имя определяется по каждой папке.",
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
            text="Текущий режим рассчитан на GTA V Legacy. Все игровые изменения будут направлены только в mods; OpenIV откроется для подтверждения.",
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

    def start_install(self):
        source_text = self.source_var.get().strip()
        game_text = self.game_var.get().strip()
        if not source_text or not game_text:
            messagebox.showwarning(
                "Не все данные указаны",
                "Выбери архив/папку с DLC и корневую папку GTA V.",
            )
            return

        source = Path(source_text)
        game = Path(game_text)
        if not source.exists() or not game.is_dir():
            messagebox.showerror("Ошибка", "Проверь выбранные пути.")
            return

        try:
            manual_name = safe_name(self.name_var.get()) if self.name_var.get().strip() else None
        except Exception as exc:
            messagebox.showerror("Проверка не пройдена", str(exc))
            return

        self.install_button.configure(state="disabled")
        self.status.set("Проверка архива и подготовка пакета OpenIV…")
        threading.Thread(
            target=self._install_worker,
            args=(source, game, manual_name),
            daemon=True,
        ).start()

    def _install_worker(self, source, game, manual_name):
        try:
            game_root = game.resolve()
            edition = validate_game_edition(game_root)
            with tempfile.TemporaryDirectory(prefix="gtav_addon_") as temp_dir:
                extraction_dir = Path(temp_dir) / "extracted"
                extraction_dir.mkdir()
                packs = prepare_sources(source, extraction_dir)

                if manual_name and len(packs) != 1:
                    raise ValueError(
                        "Поле «Имя DLC» можно заполнять только при установке одной машины. "
                        "Для архива с несколькими машинами очисти это поле."
                    )
                if manual_name:
                    packs = [(packs[0][0], manual_name)]

                pack_names = [name for _, name in packs]
                if len({name.lower() for name in pack_names}) != len(pack_names):
                    raise ValueError("В архиве обнаружились повторяющиеся имена DLC.")

                mods_root = ensure_mods_root(game_root)

                mods_update = mods_root / "update"
                ensure_inside_mods(mods_update, mods_root)
                mods_update.mkdir(parents=True, exist_ok=True)

                mods_x64 = mods_update / "x64"
                ensure_inside_mods(mods_x64, mods_root)
                mods_x64.mkdir(parents=True, exist_ok=True)

                dlcpacks = mods_x64 / "dlcpacks"
                ensure_inside_mods(dlcpacks, mods_root)
                dlcpacks.mkdir(parents=True, exist_ok=True)

                destinations = [dlcpacks / name for name in pack_names]
                for destination in destinations:
                    ensure_inside_mods(destination, mods_root)
                conflicts = [
                    path for path in destinations
                    if path.exists() or path.is_symlink()
                ]
                if conflicts:
                    conflict_text = "\n".join(str(path) for path in conflicts)
                    raise FileExistsError(
                        "Установка отменена: эти папки уже существуют:\n"
                        f"{conflict_text}\n\nНичего не перезаписано."
                    )

                # The only RPF archive targeted by the OIV package is inside mods.
                mods_update_rpf, copied_from_original = ensure_mods_update_rpf(game_root)
                ensure_inside_mods(mods_update_rpf, mods_root)
                backup = backup_mods_update_rpf(mods_update_rpf, mods_root)

                package_dir = Path(tempfile.gettempdir()) / "GTA5AddonInstaller" / "Packages"
                package_name = (
                    f"Install_DLC_{datetime.now():%Y%m%d_%H%M%S}_"
                    f"{uuid.uuid4().hex[:8]}.oiv"
                )
                package_path = package_dir / package_name
                build_openiv_package(packs, package_path)

                opened = launch_openiv_package(package_path)

            lines = [
                "Пакет установки подготовлен.",
                "",
                f"Редакция игры: GTA V {edition}",
                f"DLC-пакетов найдено: {len(pack_names)}",
                *[f"• {name}" for name in pack_names],
                "",
                f"Архив для редактирования: {mods_update_rpf}",
                f"Резервная копия: {backup}",
                (
                    "Копия update.rpf создана из оригинала в mods; "
                    "оригинальный update.rpf не изменялся."
                    if copied_from_original
                    else "Используется уже существующий mods\\update\\update.rpf."
                ),
                "",
                (
                    "Файл .oiv передан приложению Windows по умолчанию. Убедись, что это OpenIV "
                    f"и в нём выбрана эта же папка GTA V: {game_root}. "
                    "Когда OpenIV спросит место установки, выбери Game folder / корень игры "
                    "(не Mods folder): пути в пакете уже начинаются с mods\\\\. Затем нажми Install."
                    if opened
                    else "Не удалось автоматически открыть OpenIV. "
                    "Дважды щёлкни по подготовленному .oiv-файлу вручную."
                ),
                f"Пакет: {package_path}",
                "",
                "Все целевые пути в пакете начинаются с mods\\.",
            ]
            result = "\n".join(lines)
            self.root.after(0, lambda result=result: self._finish(True, result))
        except Exception as exc:
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

# GTA V Add-On Installer

Небольшая Windows-программа для установки add-on DLC-машин GTA V.

## Возможности

- Принимает ZIP-архив с одним `dlc.rpf` или папку, внутри которой находится `dlc.rpf`.
- Устанавливает содержимое DLC в `GTA V/mods/update/x64/dlcpacks/<имя_dlc>/`.
- Добавляет `<Item>dlcpacks:/<имя_dlc>/</Item>` в выбранный `dlclist.xml`.
- Не добавляет повторную запись, если DLC уже зарегистрирован.
- Создаёт резервную копию XML перед изменением.
- Проверяет XML и защищает распаковку ZIP от path traversal.

## Важно про dlclist.xml

Оригинальный `dlclist.xml` находится внутри `update.rpf`:

`update/update.rpf/common/data/dlclist.xml`

Программа не редактирует RPF-контейнер напрямую. С помощью OpenIV откройте `mods/update/update.rpf`, извлеките `common/data/dlclist.xml` во временную папку и выберите этот XML в программе. После установки замените XML внутри `update.rpf` через OpenIV.

## Запуск из исходников

Требуется Python 3.10+ и Windows с Tkinter.

```bash
python app.py
```

Внешние Python-зависимости не требуются.

## Сборка EXE

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name GTA5-Addon-Installer app.py
```

Готовый EXE появится в папке `dist`.

## Использование

1. Закройте GTA V.
2. Выберите ZIP или папку машины.
3. Выберите корневую папку GTA V.
4. В OpenIV извлеките `dlclist.xml` из `mods/update/update.rpf/common/data/`.
5. Выберите извлечённый XML.
6. Укажите имя DLC (или оставьте предложенное автоматически).
7. Нажмите «УСТАНОВИТЬ МАШИНУ».
8. Если программа добавила запись, импортируйте обновлённый XML обратно в `update.rpf` через OpenIV.

Перед использованием рекомендуется сделать резервную копию папки mods.

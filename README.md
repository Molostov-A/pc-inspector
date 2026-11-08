# PC Inspector

Локальное приложение для Windows: показывает, чем заняты диски, находит мусор, дубликаты,
забытые файлы и неиспользуемые программы и помогает безопасно навести порядок (корзина или карантин с откатом).
Работать с ним можно самому через браузер или CLI, а можно поручить анализ ИИ через MCP.

> Статус: доступны локальный веб-интерфейс, сканер, семь базовых анализаторов и CLI.

## Документы

- [docs/SPEC.md](docs/SPEC.md) — техническое задание (источник истины)
- [docs/ROADMAP.md](docs/ROADMAP.md) — этапы и прогресс
- [AGENTS.md](AGENTS.md) — правила для ИИ-разработчиков

## Веб-интерфейс

Дважды щёлкните [start-web.bat](start-web.bat) в папке проекта. Батник запустит сервер
из `.venv` и откроет браузер. Для запуска вручную выполните из папки проекта в PowerShell

```powershell
.\.venv\Scripts\python.exe -m pcinspector serve --open
```

Откроется [PC Inspector](http://127.0.0.1:8765/). Если индекс пуст, нажмите **Сканировать**
и введите путь к папке. По одному пути на строку. Пустой список запускает скан всех доступных
локальных томов NTFS. После скана откройте **Обзор**, **Мусор** или **Анализаторы**.

Для отдельного индекса укажите `--db PATH`. Порт можно изменить через `--port`.
Скан можно отменить в панели прогресса. Сервер останавливается через `Ctrl+C`.
Строка состояния показывает текущую операцию. В **Обзоре** крупные файлы показаны на карте
и в таблице. Нажатие на файл на карте выделяет его в Проводнике. Папки можно открыть
кнопкой **Открыть в Проводнике** в таблице. Переключатель **Только папки** скрывает файлы.
В группе мусора показаны до 10 крупнейших путей. Для просмотра списка без группировки
откройте отчёт о мусоре в разделе **Анализаторы** и отключите группировку.

## Как поручить разработку ИИ-агенту

Откройте папку проекта в агенте (Codex / ChatGPT, Claude Code и т. п.) и напишите:

> Прочитай AGENTS.md и docs/ROADMAP.md. Выполни этап 0 и этап 1 по ТЗ из docs/SPEC.md.
> После каждого этапа покажи результат приёмки и остановись.

## Запуск CLI в PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m pcinspector --help
python -m pcinspector analyzers
```

Для первой проверки создайте временное дерево и отдельный индекс.

```powershell
$pciDemo = Join-Path $env:TEMP ("pci-demo-" + [guid]::NewGuid().ToString("N"))
$pciRoot = Join-Path $pciDemo "Пример"
$pciFile = Join-Path $pciRoot "node_modules\пример.js"
New-Item -ItemType Directory -Path (Split-Path $pciFile) -Force | Out-Null
Set-Content -LiteralPath $pciFile -Value "console.log('PC Inspector')"
(Get-Item -LiteralPath $pciFile).LastWriteTime = (Get-Date).AddYears(-2)
$pciIndex = Join-Path $pciDemo "index.db"

python -m pcinspector --db "$pciIndex" scan "$pciRoot"
python -m pcinspector --db "$pciIndex" tree "$pciRoot"
python -m pcinspector --db "$pciIndex" junk --min-size 1
python -m pcinspector --db "$pciIndex" types --by ext --json
python -m pcinspector --db "$pciIndex" forgotten_dirs --min-size 1
python -m pcinspector --db "$pciIndex" sql "SELECT path, size FROM files_v LIMIT 10"
```

`scan` без путей выбирает доступные локальные тома NTFS. `Ctrl+C` отменяет текущий корень.
Ранее завершённые корни сохраняются. `--db` переопределяет `PCI_DB`. По умолчанию индекс
создаётся в `data/index.db`. `--json` пишет UTF-8 в stdout, ход сканирования выводится в stderr.

Временные деревья создаются автоматически при запуске `python -m pytest -q`.

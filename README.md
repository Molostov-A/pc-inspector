# PC Inspector

Локальное приложение для Windows: показывает, чем заняты диски, находит мусор, дубликаты,
забытые файлы и неиспользуемые программы и помогает безопасно навести порядок (корзина или карантин с откатом).
Работать с ним можно самому через браузер или CLI, а можно поручить анализ ИИ через MCP.

> Статус: доступны локальный веб-интерфейс, сканер, анализаторы и планы уборки.

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

## Планы уборки

Добавьте путь кнопкой **В план** в «Обзоре» или «Анализаторах». В «Мусоре» можно отметить
несколько путей. В разделе **Планы** выберите действия, проверьте итог и подтвердите выполнение.
В разделе **История** действие **Откатить** возвращает объект после карантина или перемещения,
если исходный путь свободен и родительская папка доступна. Объекты из Корзины восстанавливаются
средствами Windows.

Карантин и перемещение работают в пределах одного диска. Очистка карантина по подтверждению
переносит старые элементы в Корзину. В отчёте отдельно показаны обработанный объём и освобождённое
место. Для CLI доступны `plan create|list|show|add|execute` и `quarantine list|restore|purge`.
CLI запрашивает `yes` при выполнении, восстановлении и очистке. Для сценариев есть флаг `--yes`.

## Поиск дубликатов

После сканирования откройте **Дубликаты** и нажмите **Найти дубликаты**. Пустой корень означает
весь индекс. По умолчанию проверяются файлы от 1 МиБ. Задачу можно отменить из панели прогресса.
Повторный поиск использует хэши неизменившихся файлов. В группе выберите **Оставить**,
затем нажмите **Добавить остальные в план**. Показанный размер — потенциально освобождаемый объём.

Тот же сценарий доступен в PowerShell.

```powershell
.\.venv\Scripts\python.exe -m pcinspector hash --min-size 1M
.\.venv\Scripts\python.exe -m pcinspector duplicates --min-size 1M
```

Для проверки одной папки добавьте `--root PATH` к обеим командам.

## Установленные программы

Откройте **Забытое → Неиспользуемые программы** и нажмите **Обновить список**.
Ссылка **Все программы** показывает собранные записи независимо от даты запуска.
Сбор читает классические записи Uninstall из трёх представлений реестра. Размер папки
установки и даты доступа к EXE берутся из индекса, если папка была просканирована.

Отчёт указывает источник даты и степень достоверности. Дата Prefetch приблизительна,
а дата доступа к EXE — косвенный признак. Вкладка **Возможные остатки** показывает
папки без найденного владельца. Для записей из ProgramData доступен просмотр.

Кнопка **Открыть деинсталлятор** требует подтверждения. Для тихих или нестандартных
команд используйте «Программы и компоненты» Windows.

```powershell
.\.venv\Scripts\python.exe -m pcinspector apps
.\.venv\Scripts\python.exe -m pcinspector unused_apps --days 0 --limit 500 --json
```

## Сводка и MCP

Команда для Markdown-сводки по текущему индексу

```powershell
.\.venv\Scripts\python.exe -m pcinspector brief
```

При обновлении старой установки выполните `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`.
Сервер MCP работает через stdio. Для отдельной базы передайте после `mcp` параметр `--db`
с абсолютным путём к SQLite-индексу.

Для **Claude Desktop** добавьте сервер в `%APPDATA%\Claude\claude_desktop_config.json` и перезапустите клиент.
[Документация MCP SDK](https://py.sdk.modelcontextprotocol.io/get-started/real-host/)

```json
{
  "mcpServers": {
    "pc-inspector": {
      "command": "B:\\Programm\\pc-inspector\\.venv\\Scripts\\python.exe",
      "args": ["-m", "pcinspector", "mcp"]
    }
  }
}
```

Для **Claude Code** зарегистрируйте сервер в папке проекта и проверьте подключение.
[Документация Claude Code](https://code.claude.com/docs/en/mcp)

```powershell
claude mcp add --transport stdio pc-inspector -- "B:\Programm\pc-inspector\.venv\Scripts\python.exe" -m pcinspector mcp
claude mcp get pc-inspector
```

Пример запроса в Claude — «Используй PC Inspector и объясни, чем занят диск C и что стоит проверить перед уборкой».
Claude также может создать черновик. В веб-интерфейсе он помечен **ИИ**. Для выполнения
откройте план в интерфейсе и подтвердите его самостоятельно.

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

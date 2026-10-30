<?php
require __DIR__ . '/bootstrap.php';

$columns = [
    'man backlog',
    'llm plan',
    'llm planing',
    'preflight',
    'man plan',
    'llm todo',
    'llm doing',
    'ready-to-test',
    'llm test',
    'llm testing',
    'man test',
    'man done',
    'man canceled',
];

$llmCompactColumns = [
    'llm plan',
    'llm planing',
    'preflight',
    'llm todo',
    'llm doing',
    'llm test',
    'llm testing',
];

function db(): PDO {
    static $pdo = null;
    if ($pdo) return $pdo;
    $pdo = new PDO('sqlite:' . DB_PATH);
    $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $pdo->exec('PRAGMA busy_timeout=30000');
    $pdo->exec('PRAGMA journal_mode=WAL');
    $pdo->exec(file_get_contents(dirname(__DIR__) . '/migrations/001_board.sql'));
    migrate_project_column($pdo);
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_cards_project_position ON cards(project, position, id)');
    return $pdo;
}

function now(): string { return gmdate('Y-m-d H:i:s') . ' UTC'; }

function h(string $s): string { return htmlspecialchars($s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8'); }

function board_url(?string $project = null, array $extra = []): string {
    $params = $extra;
    if ($project !== null) $params['project'] = $project === '' ? 'unassigned' : $project;
    $query = $params ? '?' . http_build_query($params) : '';
    return BASE . '/' . $query;
}

function redirect_board(?string $project = null): never {
    header('Location: ' . board_url($project ?? project_filter_from_request()));
    exit;
}

function wants_json(): bool {
    return str_contains($_SERVER['HTTP_ACCEPT'] ?? '', 'application/json')
        || strtolower($_SERVER['HTTP_X_REQUESTED_WITH'] ?? '') === 'fetch';
}

function json_ok(array $extra = []): never {
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['ok' => true] + $extra, JSON_UNESCAPED_UNICODE);
    exit;
}

function event_log(?int $cardId, string $type, string $payload = ''): void {
    $stmt = db()->prepare('INSERT INTO events(card_id,event_type,payload,created_at) VALUES (?,?,?,?)');
    $stmt->execute([$cardId, $type, $payload, now()]);
}

function normalize_project(string $project): string {
    $project = strtolower(trim($project));
    if ($project === '' || $project === 'all' || $project === 'unassigned') return '';
    $project = str_replace('_', '-', $project);
    $project = preg_replace('/[^a-z0-9-]+/', '-', $project);
    $project = trim((string)$project, '-');
    return preg_match('/^[a-z0-9][a-z0-9-]{0,63}$/', $project) ? $project : '';
}

function infer_project(string $title): string {
    $title = trim($title);
    if ($title === '') return '';
    if (!preg_match('/^([^:]+):/', $title, $m)) return '';
    $prefix = normalize_project($m[1]);
    if ($prefix === '') return '';
    return $prefix;
}

function migrate_project_column(PDO $pdo): void {
    $columns = $pdo->query('PRAGMA table_info(cards)')->fetchAll(PDO::FETCH_ASSOC);
    $hasProject = false;
    foreach ($columns as $column) {
        if (($column['name'] ?? '') === 'project') {
            $hasProject = true;
            break;
        }
    }
    if (!$hasProject) {
        $pdo->exec('ALTER TABLE cards ADD COLUMN project TEXT NOT NULL DEFAULT ""');
        $cards = $pdo->query('SELECT id,title FROM cards WHERE project=""')->fetchAll(PDO::FETCH_ASSOC);
        $stmt = $pdo->prepare('UPDATE cards SET project=? WHERE id=?');
        foreach ($cards as $card) $stmt->execute([infer_project((string)$card['title']), (int)$card['id']]);
    }
}

function load_cards(?string $project = null): array {
    if ($project === null) {
        $stmt = db()->query('SELECT * FROM cards ORDER BY position ASC, id ASC');
    } else {
        $stmt = db()->prepare('SELECT * FROM cards WHERE project=? ORDER BY position ASC, id ASC');
        $stmt->execute([$project]);
    }
    return $stmt->fetchAll(PDO::FETCH_ASSOC);
}

function load_card(int $id): ?array {
    $stmt = db()->prepare('SELECT * FROM cards WHERE id=?');
    $stmt->execute([$id]);
    $card = $stmt->fetch(PDO::FETCH_ASSOC);
    return $card ?: null;
}

function load_projects(): array {
    $stmt = db()->query('SELECT project, COUNT(*) AS count FROM cards GROUP BY project ORDER BY project ASC');
    return $stmt->fetchAll(PDO::FETCH_ASSOC);
}

function table_exists(string $name): bool {
    $stmt = db()->prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?");
    $stmt->execute([$name]);
    return (bool)$stmt->fetchColumn();
}

function work_active_statuses(): array {
    return ['approved','queued','claiming','active','waiting','blocked','verifying','handoff','stale'];
}

function work_path_overlap(string $left, string $right): bool {
    return $left === $right || str_starts_with($left, $right . '/') || str_starts_with($right, $left . '/');
}

function work_resources_overlap(array $left, array $right): bool {
    if (($left['access_mode'] ?? '') === 'read' && ($right['access_mode'] ?? '') === 'read') return false;
    $pathTypes = ['repo','worktree','path'];
    $leftType = (string)$left['resource_type'];
    $rightType = (string)$right['resource_type'];
    $leftValue = (string)$left['normalized_selector'];
    $rightValue = (string)$right['normalized_selector'];
    if (in_array($leftType, $pathTypes, true) && in_array($rightType, $pathTypes, true)) {
        return work_path_overlap($leftValue, $rightValue);
    }
    return $leftType === $rightType && $leftValue === $rightValue;
}

function load_current_work(bool $includeExpired = false): array {
    if (!table_exists('work_plans')) return [];
    $statuses = work_active_statuses();
    $marks = implode(',', array_fill(0, count($statuses), '?'));
    $sql = "SELECT * FROM work_plans WHERE status IN ($marks)";
    $params = $statuses;
    if (!$includeExpired) {
        $now = gmdate('Y-m-d\TH:i:s\Z');
        $sql .= " AND (lease_expires_at > ? OR lease_expires_at IS NULL OR lease_expires_at = '')";
        $params[] = $now;
    }
    $sql .= " ORDER BY updated_at DESC";
    $stmt = db()->prepare($sql);
    $stmt->execute($params);
    $plans = $stmt->fetchAll(PDO::FETCH_ASSOC);
    $resourceStmt = db()->prepare('SELECT * FROM work_resources WHERE plan_id=? ORDER BY id');
    $stepStmt = db()->prepare('SELECT * FROM work_steps WHERE plan_id=? ORDER BY position,id');
    foreach ($plans as &$plan) {
        $resourceStmt->execute([$plan['id']]);
        $plan['resources'] = $resourceStmt->fetchAll(PDO::FETCH_ASSOC);
        $stepStmt->execute([$plan['id']]);
        $plan['steps'] = $stepStmt->fetchAll(PDO::FETCH_ASSOC);
        $plan['warnings'] = [];
    }
    unset($plan);
    $count = count($plans);
    for ($i = 0; $i < $count; $i++) {
        for ($j = $i + 1; $j < $count; $j++) {
            $overlaps = [];
            foreach ($plans[$i]['resources'] as $left) {
                foreach ($plans[$j]['resources'] as $right) {
                    if (work_resources_overlap($left, $right)) {
                        $overlaps[] = $left['resource_type'] . ':' . $left['normalized_selector'];
                    }
                }
            }
            if ($overlaps) {
                $warningI = ['severity' => 'overlap', 'task_name' => $plans[$j]['task_name'], 'resources' => array_values(array_unique($overlaps))];
                $warningJ = ['severity' => 'overlap', 'task_name' => $plans[$i]['task_name'], 'resources' => array_values(array_unique($overlaps))];
                $plans[$i]['warnings'][] = $warningI;
                $plans[$j]['warnings'][] = $warningJ;
            } elseif ($plans[$i]['project'] === $plans[$j]['project']) {
                $plans[$i]['warnings'][] = ['severity' => 'same-project', 'task_name' => $plans[$j]['task_name'], 'resources' => []];
                $plans[$j]['warnings'][] = ['severity' => 'same-project', 'task_name' => $plans[$i]['task_name'], 'resources' => []];
            }
        }
    }
    return $plans;
}

function load_work_plan(string $id): ?array {
    if (!table_exists('work_plans') || !preg_match('/\A[a-zA-Z0-9_-]{1,128}\z/', $id)) return null;
    $stmt = db()->prepare('SELECT * FROM work_plans WHERE id=?');
    $stmt->execute([$id]);
    $plan = $stmt->fetch(PDO::FETCH_ASSOC);
    if (!$plan) return null;
    $steps = db()->prepare('SELECT * FROM work_steps WHERE plan_id=? ORDER BY position,id');
    $steps->execute([$id]);
    $plan['steps'] = $steps->fetchAll(PDO::FETCH_ASSOC);
    return $plan;
}

function project_filter_from_request(): ?string {
    if (!isset($_GET['project'])) return null;
    $project = trim((string)$_GET['project']);
    if ($project === '' || $project === 'all') return null;
    if ($project === 'unassigned') return '';
    return normalize_project($project);
}

function get_setting(string $key, string $default = ''): string {
    $stmt = db()->prepare('SELECT value FROM settings WHERE key=?');
    $stmt->execute([$key]);
    $value = $stmt->fetchColumn();
    return $value === false ? $default : (string)$value;
}

function set_setting(string $key, string $value): void {
    $stmt = db()->prepare('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value');
    $stmt->execute([$key, $value]);
}

function normalize_language(string $language): string {
    return in_array($language, ['Russian', 'English'], true) ? $language : 'Russian';
}

function normalize_column(string $column, array $columns): string {
    return in_array($column, $columns, true) ? $column : $columns[0];
}

function handle_post(array $columns): void {
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') return;
    verify_csrf();
    $action = $_POST['action'] ?? '';
    if (!in_array($action, ['settings','create','update','move'], true)) {
        http_response_code(400); exit('Unknown action');
    }
    if (isset($_POST['column_name']) && !in_array($_POST['column_name'], $columns, true)) {
        http_response_code(422); exit('Unknown column');
    }
    if ($action === 'settings') {
        set_setting('language', normalize_language((string)($_POST['language'] ?? 'Russian')));
        header('Location: ' . board_url(project_filter_from_request(), ['settings' => '1']));
        exit;
    }
    if ($action === 'create') {
        $title = trim((string)($_POST['title'] ?? ''));
        $description = trim((string)($_POST['description'] ?? ''));
        $column = normalize_column((string)($_POST['column_name'] ?? 'man backlog'), $columns);
        if ($title !== '') {
            $project = normalize_project((string)($_POST['project'] ?? ''));
            if ($project === '') $project = infer_project($title);
            $stmtMax = db()->prepare('SELECT COALESCE(MAX(position),0)+1 FROM cards WHERE column_name=?');
            $stmtMax->execute([$column]);
            $pos = (int)$stmtMax->fetchColumn();
            $stmt = db()->prepare('INSERT INTO cards(title,description,column_name,project,position,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)');
            $stmt->execute([$title, $description, $column, $project, $pos, 'web', now(), now()]);
            event_log((int)db()->lastInsertId(), 'create', json_encode(['column' => $column, 'project' => $project], JSON_UNESCAPED_UNICODE));
            redirect_board($project);
        }
        redirect_board();
    }
    if ($action === 'update') {
        $id = (int)($_POST['id'] ?? 0);
        $title = trim((string)($_POST['title'] ?? ''));
        $description = trim((string)($_POST['description'] ?? ''));
        $column = normalize_column((string)($_POST['column_name'] ?? 'man backlog'), $columns);
        if ($id > 0 && $title !== '') {
            $project = normalize_project((string)($_POST['project'] ?? ''));
            $stmt = db()->prepare('UPDATE cards SET title=?, description=?, column_name=?, project=?, updated_at=? WHERE id=?');
            $stmt->execute([$title, $description, $column, $project, now(), $id]);
            event_log($id, 'update', json_encode(['column' => $column, 'project' => $project], JSON_UNESCAPED_UNICODE));
            redirect_board($project);
        }
        redirect_board();
    }
    if ($action === 'move') {
        $id = (int)($_POST['id'] ?? 0);
        $column = normalize_column((string)($_POST['column_name'] ?? 'man backlog'), $columns);
        if ($id > 0) {
            $stmtMax = db()->prepare('SELECT COALESCE(MAX(position),0)+1 FROM cards WHERE column_name=?');
            $stmtMax->execute([$column]);
            $pos = (int)$stmtMax->fetchColumn();
            $stmt = db()->prepare('UPDATE cards SET column_name=?, position=?, updated_at=? WHERE id=?');
            $stmt->execute([$column, $pos, now(), $id]);
            event_log($id, 'move', $column);
        }
        if (wants_json()) json_ok(['id' => $id, 'column_name' => $column]);
        redirect_board();
    }
}

function api_response(array $columns): void {
    $path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '';
    if (!str_starts_with($path, BASE . '/api/')) return;
    header('Content-Type: application/json; charset=utf-8');
    if ($path === BASE . '/api/cards') {
        $activeProject = project_filter_from_request();
        echo json_encode([
            'cards' => load_cards($activeProject),
            'columns' => $columns,
            'projects' => load_projects(),
            'active_project' => $activeProject,
        ], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
        exit;
    }
    if ($path === BASE . '/api/work') {
        $includeAll = isset($_GET['all']) && $_GET['all'] === '1';
        echo json_encode([
            'mode' => 'audit-only',
            'plans' => load_current_work($includeAll),
        ], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
        exit;
    }
    http_response_code(404);
    echo json_encode(['error' => 'not found'], JSON_UNESCAPED_UNICODE);
    exit;
}

db();
api_response($columns);
handle_post($columns);
$activeProject = project_filter_from_request();
$projects = load_projects();
$cards = load_cards($activeProject);
$byColumn = array_fill_keys($columns, []);
foreach ($cards as $card) $byColumn[$card['column_name']][] = $card;
$editId = isset($_GET['edit']) ? (int)$_GET['edit'] : 0;
$actionNew = isset($_GET['action']) && $_GET['action'] === 'new';
$editCard = $editId > 0 ? load_card($editId) : null;
$formProject = $editCard['project'] ?? ($activeProject ?? '');
$currentLanguage = normalize_language(get_setting('language', 'English'));
$showSettings = isset($_GET['settings']);
$showWork = (string)($_GET['view'] ?? '') === 'work';
$showAllWork = isset($_GET['all']) && $_GET['all'] === '1';
$workPlanId = $showWork ? (string)($_GET['plan'] ?? '') : '';
$workDetail = $workPlanId !== '' ? load_work_plan($workPlanId) : null;
if ($workPlanId !== '' && !$workDetail) http_response_code(404);
$currentWork = $showWork && $workPlanId === '' ? load_current_work($showAllWork) : [];
?>
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLMBAN</title>
<link rel="icon" href="<?= h(BASE) ?>/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#0f1115;--panel:#171a21;--card:#222733;--card2:#1d2230;--text:#e8eaf0;--muted:#9ba3b4;--line:#303748;--accent:#8ab4ff;--ok:#8ee6a2}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}a{color:var(--accent);text-decoration:none}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;position:sticky;top:0;background:rgba(15,17,21,.96);z-index:3;flex-wrap:wrap}h1{font-size:20px;margin:0}.muted{color:var(--muted)}
.project-filter{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.project-pill{background:#1b2030;border:1px solid var(--line);border-radius:999px;color:var(--muted);padding:3px 9px}.project-pill:hover,.project-pill.active{background:#28344b;color:var(--text);border-color:#4a5a76}
.board{display:flex;gap:12px;overflow-x:auto;padding:14px;align-items:flex-start}.col{background:var(--panel);border:1px solid var(--line);border-radius:10px;min-width:280px;width:280px;max-height:calc(100vh - 82px);overflow:auto;transition:min-width .16s ease,width .16s ease,background .16s ease}.col.drag-over{outline:2px solid var(--accent);outline-offset:2px}.col h2{font-size:14px;margin:0;padding:10px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:1}.col-title{display:flex;align-items:center}.col.compact{min-width:54px;width:54px;overflow:hidden;background:#141821}.col.compact h2{height:220px;padding:10px 6px;border-bottom:0;writing-mode:vertical-rl;text-orientation:mixed;justify-content:flex-start;gap:8px;white-space:nowrap}.col.compact .col-count{margin-left:0;color:var(--accent)}.col.compact.drag-over,.col.compact:focus-within{min-width:180px;width:180px;overflow:auto}.col.compact.drag-over h2,.col.compact:focus-within h2{height:auto;writing-mode:horizontal-tb;padding:10px 12px;border-bottom:1px solid var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;margin:8px;padding:9px;cursor:grab}.card:active{cursor:grabbing}.card.dragging{opacity:.5}.card h3{font-size:14px;line-height:1.25;margin:0;font-weight:normal}.card-title{display:block;color:var(--text)}.card-title:hover{color:var(--accent)}.card-project{display:inline-block;margin-top:6px;color:var(--muted);font-size:12px}
.panel{margin:14px;padding:14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;max-width:900px}
input,textarea,select{width:100%;background:#10131a;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px;font:inherit}textarea{min-height:140px;margin-top:10px}.grid{display:grid;gap:10px}.row{display:grid;grid-template-columns:1fr 220px 180px;gap:10px}@media(max-width:800px){.row{grid-template-columns:1fr}.board{padding:8px;gap:8px}.col{min-width:86vw}.col.compact{min-width:46px;width:46px}.col.compact.drag-over,.col.compact:focus-within{min-width:70vw;width:70vw}}
.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:9}.panel.modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:10;margin:0;width:98vw;height:96vh;max-width:none;box-shadow:0 10px 30px rgba(0,0,0,.5);display:flex;flex-direction:column}.panel.modal form{flex:1;display:flex;flex-direction:column;gap:10px}.panel.modal textarea{flex:1;margin-top:0}
.btn,button{background:#2b3344;color:var(--text);border:1px solid #3b455a;border-radius:7px;padding:5px 12px;cursor:pointer;font:inherit;display:inline-block;text-align:center}.btn:hover,button:hover{background:#354059}.add-btn{font-weight:normal;font-size:18px;line-height:1;margin-left:auto}
.work-list{display:grid;gap:12px;padding:14px;max-width:1400px}.work-card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}.work-card.warning{border-color:#b8893c}.work-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 8px;color:var(--muted)}.badge.warn{border-color:#b8893c;color:#ffd98d}.work-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:12px}.work-grid h3{font-size:13px;color:var(--muted);margin:0 0 5px}.work-grid ul{margin:0;padding-left:20px}.work-empty{padding:32px;color:var(--muted)}
</style>
</head>
<body>
<header><h1>LLMBAN</h1><span class="muted">simple LLM kanban</span><a href="<?= BASE ?>/">Board</a><a href="<?= BASE ?>/?view=work">Current Work</a><a href="<?= $showWork ? BASE . '/api/work' : BASE . '/api/cards' . ($activeProject === null ? '' : '?project=' . h($activeProject === '' ? 'unassigned' : $activeProject)) ?>"><?= $showWork ? 'api/work' : 'api/cards' ?></a><a href="<?= board_url($activeProject, ['settings' => '1']) ?>">Settings</a><?php if (!$showWork): ?><nav class="project-filter"><a class="project-pill <?= $activeProject === null ? 'active' : '' ?>" href="<?= board_url(null) ?>">All</a><?php foreach ($projects as $projectRow): $project = (string)$projectRow['project']; $projectParam = $project === '' ? 'unassigned' : $project; $projectLabel = $project === '' ? 'Unassigned' : $project; ?><a class="project-pill <?= $activeProject === $project ? 'active' : '' ?>" href="<?= board_url($project) ?>"><?= h($projectLabel) ?> · <?= (int)$projectRow['count'] ?></a><?php endforeach; ?></nav><?php endif; ?></header>

<?php if ($showSettings): ?>
<div class="modal-overlay" onclick="location.href='<?= board_url($activeProject) ?>'"></div>
<section class="panel modal" style="height:auto;max-width:520px">
<form method="post" class="grid">
<input type="hidden" name="csrf" value="<?= h($_SESSION['csrf']) ?>">
<input type="hidden" name="action" value="settings">
<h2 style="margin-top:0">Settings</h2>
<label>Output language for LLM agents
<select name="language">
  <option value="Russian" <?= $currentLanguage === 'Russian' ? 'selected' : '' ?>>Русский</option>
  <option value="English" <?= $currentLanguage === 'English' ? 'selected' : '' ?>>English</option>
</select>
</label>
<div style="display:flex;gap:10px;align-items:center;margin-top:10px">
  <button type="submit">Save settings</button>
  <a class="btn" href="<?= board_url($activeProject) ?>">Cancel</a>
</div>
</form>
</section>
<?php endif; ?>

<?php if ($editCard || $actionNew): ?>
<div class="modal-overlay" onclick="location.href='<?= board_url($activeProject) ?>'"></div>
<section class="panel modal">
<form method="post" class="grid">
<input type="hidden" name="csrf" value="<?= h($_SESSION['csrf']) ?>">
<input type="hidden" name="action" value="<?= $editCard ? 'update' : 'create' ?>">
<?php if ($editCard): ?><input type="hidden" name="id" value="<?= (int)$editCard['id'] ?>"><?php endif; ?>
<div class="row"><input name="title" placeholder="Task title" value="<?= h($editCard['title'] ?? '') ?>" required autofocus><select name="column_name"><?php foreach ($columns as $c): ?><option value="<?= h($c) ?>" <?= (($editCard['column_name'] ?? 'man backlog') === $c) ? 'selected' : '' ?>><?= h($c) ?></option><?php endforeach; ?></select><input name="project" placeholder="Project slug" value="<?= h($formProject) ?>"></div>
<textarea name="description" placeholder="Description"><?= h($editCard['description'] ?? '') ?></textarea>
<div style="display:flex;gap:10px;align-items:center;margin-top:10px">
  <button type="submit"><?= $editCard ? 'Save changes' : 'Create card' ?></button>
  <a class="btn" href="<?= board_url($activeProject) ?>">Cancel</a>
  <?php if ($editCard): ?>
    <span class="muted" style="margin-left:auto">#<?= (int)$editCard['id'] ?> · updated <?= h($editCard['updated_at']) ?></span>
  <?php endif; ?>
</div>
</form>
</section>
<?php endif; ?>

<?php if ($showWork): ?>
<main class="work-list">
<?php if ($workPlanId !== ''): ?>
<?php if (!$workDetail): ?><div class="work-empty">Task not found.</div><?php else: ?>
<article class="work-card">
  <a href="<?= BASE ?>/?view=work">← Current Work</a>
  <div class="work-head"><h2><?= h($workDetail['task_name']) ?></h2><span class="badge"><?= h($workDetail['status']) ?></span></div>
  <p class="muted"><?= h($workDetail['owner']) ?> / <?= h($workDetail['harness']) ?> · <?= h($workDetail['hostname']) ?> · <?= h($workDetail['updated_at']) ?></p>
  <p><strong>Goal:</strong> <?= h($workDetail['goal']) ?></p>
  <?php if ($workDetail['acceptance_criteria']): ?><p><strong>Acceptance:</strong> <?= h($workDetail['acceptance_criteria']) ?></p><?php endif; ?>
  <p><strong>Current:</strong> <?= h($workDetail['current_step'] ?: '-') ?></p>
  <?php if ($workDetail['next_action']): ?><p><strong>Next:</strong> <?= h($workDetail['next_action']) ?></p><?php endif; ?>
  <h3>Steps and checks</h3>
  <?php if (!$workDetail['steps']): ?><p class="muted">No steps recorded.</p><?php endif; ?>
  <?php foreach ($workDetail['steps'] as $step): ?>
  <section><p><span class="badge"><?= h($step['status']) ?></span> <?= h($step['title']) ?></p>
  <?php if ($step['evidence']): ?><p><?= nl2br(h($step['evidence'])) ?></p><?php endif; ?></section>
  <?php endforeach; ?>
</article>
<?php endif; ?>
<?php else: ?>
<div style="display:flex;gap:10px;align-items:center;margin-bottom:8px">
  <a class="btn" style="<?= !$showAllWork ? 'background:var(--accent);color:#0f1115;font-weight:bold;' : '' ?>" href="<?= BASE ?>/?view=work">Live leases</a>
  <a class="btn" style="<?= $showAllWork ? 'background:var(--accent);color:#0f1115;font-weight:bold;' : '' ?>" href="<?= BASE ?>/?view=work&amp;all=1">All unfinished</a>
</div>
<?php if (!$currentWork): ?><div class="work-empty">No active registered work. Registry mode: audit-only.</div><?php endif; ?>
<?php foreach ($currentWork as $work): ?>
<article class="work-card<?= count($work['warnings']) ? ' warning' : '' ?>">
  <div class="work-head"><h2 style="margin:0;font-size:18px"><a href="<?= BASE ?>/?view=work&amp;plan=<?= rawurlencode($work['id']) ?>"><?= h($work['task_name']) ?></a></h2><span class="badge"><?= h($work['status']) ?></span><span class="badge"><?= h($work['project']) ?></span><?php if (count($work['warnings'])): ?><span class="badge warn"><?= count($work['warnings']) ?> warning(s)</span><?php endif; ?></div>
  <p class="muted"><?= h($work['owner']) ?> / <?= h($work['harness']) ?> · <?= h($work['hostname']) ?>:<?= h($work['cwd']) ?> · heartbeat <?= h($work['heartbeat_at']) ?> · lease <?= h($work['lease_expires_at']) ?></p>
  <p><strong>Current:</strong> <?= h($work['current_step'] ?: '-') ?> <span class="muted">Next: <?= h($work['next_action'] ?: '-') ?></span></p>
  <div class="work-grid">
    <section><h3>Resources</h3><ul><?php foreach ($work['resources'] as $resource): ?><li><code><?= h($resource['access_mode']) ?> <?= h($resource['resource_type']) ?>:<?= h($resource['normalized_selector']) ?></code></li><?php endforeach; ?><?php if (!$work['resources']): ?><li>none declared</li><?php endif; ?></ul></section>
    <section><h3>Steps</h3><ul><?php foreach ($work['steps'] as $step): ?><li><code><?= h($step['status']) ?></code> <?= h($step['title']) ?></li><?php endforeach; ?><?php if (!$work['steps']): ?><li>none registered</li><?php endif; ?></ul></section>
    <section><h3>Coordination</h3><ul><?php foreach ($work['warnings'] as $warning): ?><li><code><?= h($warning['severity']) ?></code> <?= h($warning['task_name']) ?><?= $warning['resources'] ? ' · ' . h(implode(', ', $warning['resources'])) : '' ?></li><?php endforeach; ?><?php if (!$work['warnings']): ?><li>no warnings</li><?php endif; ?></ul></section>
  </div>
</article>
<?php endforeach; ?>
<?php endif; ?>
</main>
<?php else: ?>
<main class="board">
<?php foreach ($columns as $col): $cardCount = count($byColumn[$col]); $isCompactLlm = in_array($col, $llmCompactColumns, true) && $cardCount === 0; ?>
<section class="col<?= $isCompactLlm ? ' compact' : '' ?>" data-column="<?= h($col) ?>" data-compact="<?= $isCompactLlm ? '1' : '0' ?>" title="<?= h($col) ?> · <?= $cardCount ?>">
  <h2 class="col-title">
    <?= h($col) ?> <span class="muted col-count">· <?= $cardCount ?></span>
    <?php if ($col === 'man backlog'): ?>
      <a href="<?= board_url($activeProject, ['action' => 'new']) ?>" class="add-btn" title="Add card">+</a>
    <?php endif; ?>
  </h2>
<?php foreach ($byColumn[$col] as $card): ?>
<article class="card" draggable="true" data-card-id="<?= (int)$card['id'] ?>">
  <h3><a class="card-title" href="<?= board_url($activeProject, ['edit' => (int)$card['id']]) ?>">#<?= (int)$card['id'] ?> <?= h($card['title']) ?></a></h3><?php if (($card['project'] ?? '') !== ''): ?><span class="card-project"><?= h($card['project']) ?></span><?php endif; ?>
</article>
<?php endforeach; ?>
</section>
<?php endforeach; ?>
</main>
<?php endif; ?>

<script>
const base = <?= json_encode(BASE) ?>;
const boardUrl = <?= json_encode(board_url($activeProject)) ?>;
let dragged = null;

document.querySelectorAll('.card').forEach(card => {
  card.addEventListener('dragstart', event => {
    dragged = card;
    card.classList.add('dragging');
    event.dataTransfer.setData('text/plain', card.dataset.cardId);
    event.dataTransfer.effectAllowed = 'move';
  });
  card.addEventListener('dragend', () => card.classList.remove('dragging'));
});

document.querySelectorAll('.col').forEach(col => {
  col.addEventListener('dragover', event => {
    event.preventDefault();
    col.classList.add('drag-over');
  });
  col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
  col.addEventListener('drop', async event => {
    event.preventDefault();
    col.classList.remove('drag-over');
    const id = event.dataTransfer.getData('text/plain');
    if (!id) return;
    await moveCard(id, col.dataset.column);
  });
});

async function moveCard(id, column) {
  const form = new FormData();
  form.append('csrf', <?= json_encode($_SESSION['csrf']) ?>);
  form.append('action', 'move');
  form.append('id', id);
  form.append('column_name', column);
  const response = await fetch(base + '/', {
    method: 'POST',
    headers: {'Accept': 'application/json', 'X-Requested-With': 'fetch'},
    body: form
  });
  if (!response.ok) {
    alert('move failed');
    return;
  }
  location.href = boardUrl;
}
</script>
</body>
</html>

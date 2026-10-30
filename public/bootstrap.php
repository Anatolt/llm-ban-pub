<?php
declare(strict_types=1);

define('DB_PATH', getenv('LLMBAN_DB') ?: dirname(__DIR__) . '/data/llmban.sqlite');
$base = rtrim(getenv('LLMBAN_BASE_PATH') ?: '', '/');
if (!preg_match('~\A(?:/[a-zA-Z0-9_-]+)*\z~', $base)) {
    http_response_code(500); exit('Invalid LLMBAN_BASE_PATH');
}
define('BASE', $base);
header('X-Content-Type-Options: nosniff');
header('X-Frame-Options: DENY');
header('Referrer-Policy: same-origin');
header('Cache-Control: no-store');
$user = getenv('LLMBAN_USERNAME') ?: '';
$password = getenv('LLMBAN_PASSWORD') ?: '';
if ($user === '' || strlen($password) < 16) {
    http_response_code(503);
    exit('Set LLMBAN_USERNAME and LLMBAN_PASSWORD (at least 16 characters).');
}
if (!hash_equals($user, $_SERVER['PHP_AUTH_USER'] ?? '') ||
    !hash_equals($password, $_SERVER['PHP_AUTH_PW'] ?? '')) {
    header('WWW-Authenticate: Basic realm="LLM BAN", charset="UTF-8"');
    http_response_code(401); exit('Authentication required');
}
session_name('llmban_session');
session_start([
    'cookie_httponly' => true,
    'cookie_samesite' => 'Strict',
    'cookie_secure' => getenv('LLMBAN_COOKIE_SECURE') === '1',
    'cookie_path' => BASE . '/',
    'use_strict_mode' => true,
]);
$_SESSION['csrf'] ??= bin2hex(random_bytes(32));
function verify_csrf(): void {
    if (!is_string($_POST['csrf'] ?? null) || !hash_equals($_SESSION['csrf'], $_POST['csrf'])) {
        http_response_code(403); exit('Invalid CSRF token');
    }
}
if (!is_dir(dirname(DB_PATH))) mkdir(dirname(DB_PATH), 0770, true);

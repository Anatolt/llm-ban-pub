<?php
// PHP development server only. Apache uses .htaccess.
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
if ($path === '/favicon.svg') return false;
require __DIR__ . '/index.php';

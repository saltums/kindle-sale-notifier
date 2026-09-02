# Kindle Sale Notifier セットアップスクリプト
# PowerShell で実行: cd "C:\Users\sugar\Claude\kindle_notifier"; .\setup.ps1

$scriptDir = $PSScriptRoot
$pythonScript = Join-Path $scriptDir "kindle_check.py"

Write-Host "=== Kindle Sale Notifier セットアップ ===" -ForegroundColor Cyan

# 依存パッケージのインストール
Write-Host "`n[1/3] Python パッケージをインストール中..."
pip install -r "$scriptDir\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install 失敗。Python がインストールされているか確認してください。" -ForegroundColor Red
    exit 1
}

# テスト実行
Write-Host "`n[2/3] 動作テスト中..."
python $pythonScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "スクリプトの実行に失敗しました。エラーを確認してください。" -ForegroundColor Red
    exit 1
}

# Windows タスクスケジューラに登録（毎日 9:00 に実行）
Write-Host "`n[3/3] タスクスケジューラに登録中（毎日 9:00 実行）..."
$taskName = "KindleSaleNotifier"
$action = New-ScheduledTaskAction -Execute "python" -Argument "`"$pythonScript`""
$trigger = New-ScheduledTaskTrigger -Daily -At "09:00"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 1

# 既存タスクがあれば削除
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest
if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✅ セットアップ完了！毎日 9:00 に自動チェックされます。" -ForegroundColor Green
    Write-Host "   タスク名: $taskName"
    Write-Host "   ログ: $scriptDir\check_log.txt"
} else {
    Write-Host "タスクスケジューラへの登録に失敗しました。管理者権限で実行してください。" -ForegroundColor Red
}

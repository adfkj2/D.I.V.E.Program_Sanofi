param(
    [ValidateSet('Launch','VerifyWorker','Observe','Cleanup')][string]$Mode='Observe',
    [Parameter(Mandatory=$true)][string]$Output,
    [string]$PipeName='', [int]$ServerPID=0, [long]$ServerBirth=0,
    [string]$Interpreter='', [string]$ExpectedWorkerExecutable='',
    [string]$Runner='', [string]$Spec='', [string]$Repository=''
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
$repoRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
$reports=(Resolve-Path -LiteralPath (Join-Path $repoRoot 'eval/reports')).Path
$outputPath=(Resolve-Path -LiteralPath $Output).Path
if (-not $outputPath.StartsWith($reports+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {throw 'Output must be an existing child report directory'}
$controlPath=Join-Path $outputPath 'scheduler-control.json'

function Write-Evidence($Path,$Value,[bool]$Exclusive=$false) {
    $encoded=[Text.UTF8Encoding]::new($false).GetBytes(($Value|ConvertTo-Json -Depth 12))
    $fileMode=if ($Exclusive) {[IO.FileMode]::CreateNew} else {[IO.FileMode]::Create}
    $stream=[IO.File]::Open($Path,$fileMode,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$stream.Write($encoded,0,$encoded.Length)} finally {$stream.Dispose()}
}
function Digest($Text) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try {[Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$Text))).ToLowerInvariant()}
    finally {$sha.Dispose()}
}
function Owned-Record($Control) {
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if (-not $Control.created_by_this_runtime -or $Control.owner_sid -ne $sid -or $Control.task_name -notmatch '^DIVE-Validation-[0-9a-f]{32}$' -or $Control.task_path -ne '\') {throw 'No exact owned runtime task'}
}
function Current-Observation($Control) {
    Owned-Record $Control
    $task=Get-ScheduledTask -TaskName $Control.task_name -TaskPath $Control.task_path
    $info=Get-ScheduledTaskInfo -TaskName $Control.task_name -TaskPath $Control.task_path
    $xml=[string](Export-ScheduledTask -TaskName $Control.task_name -TaskPath $Control.task_path)
    $same=($task.Description -ceq $Control.ownership_marker -and (Digest $xml) -ceq $Control.registered_xml_sha256)
    $now=[datetimeoffset]::UtcNow
    $lastRun=[datetimeoffset]$info.LastRunTime.ToUniversalTime()
    $requested=[datetimeoffset]::Parse($Control.start_requested_at_utc)
    $start=($lastRun -ge $requested.AddSeconds(-2) -and $lastRun -le $now)
    $stopped=($task.State.ToString() -eq 'Ready' -and [long]$info.LastTaskResult -notin @(267009,267011))
    [ordered]@{observed_at_utc=$now.ToString('o');task_name=$Control.task_name;task_path=$Control.task_path;
        identity_verified=$same;current_start_verified=$start;stopped=$stopped;state=$task.State.ToString();
        last_run_at_utc=$lastRun.ToString('o');exit_code=if ($same -and $start -and $stopped) {[long]$info.LastTaskResult} else {$null};
        last_task_result=[long]$info.LastTaskResult;xml_sha256=(Digest $xml);
        worker_terminal_exists=(Test-Path -LiteralPath (Join-Path $outputPath 'terminal.json'));
        observer_wrote_worker_terminal=$false;automatic_retry=$false;
        semantic_result='NOT_ESTABLISHED_BY_PROCESS_EXIT';usage_policy='Preserve pending reservation; external exit cannot establish supplier usage'}
}

if ($Mode -eq 'Launch') {
    if (Test-Path -LiteralPath $controlPath) {throw 'Task control exists; never register a duplicate'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    if ($identity.Name -match 'CodexSandbox') {throw 'Task launch requires the current interactive user'}
    $prefix='\\.\pipe\DIVE-R69-'
    if (-not $PipeName.StartsWith($prefix) -or $PipeName.Substring($prefix.Length) -notmatch '^[0-9a-f]{48}$' -or $ServerPID -le 0 -or $ServerBirth -le 0) {throw 'Exact local memory server identity required'}
    $expectedRunner=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'runner.py')).Path
    if ((Resolve-Path -LiteralPath $Runner).Path -ine $expectedRunner -or (Resolve-Path -LiteralPath $Repository).Path -ine $repoRoot) {throw 'The fixed repository runtime must be used'}
    $specPath=(Resolve-Path -LiteralPath $Spec).Path
    if (-not $specPath.StartsWith($reports+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {throw 'Spec must be inside reports'}
    foreach ($exe in @($Interpreter,$ExpectedWorkerExecutable)) {
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf) -or [IO.Path]::GetFileName($exe) -ine 'pythonw.exe') {throw 'Hidden Python executable unavailable'}
    }
    $record=[ordered]@{schema_version='dive-owned-validation-task-v1';status='PREPARING';owner_sid=$identity.User.Value;
        task_name=('DIVE-Validation-'+[guid]::NewGuid().ToString('N'));task_path='\';ownership_marker=('DIVE one-shot validation '+[guid]::NewGuid().ToString('N'));
        created_by_this_runtime=$false;cleanup_required=$false;automatic_restart=$false;trigger_count=0;run_level='Limited';logon_type='Interactive';
        start_requested_at_utc=$null;registered_xml_sha256=$null;action_executable=$Interpreter;
        action_arguments=('-u "'+$expectedRunner+'" --spec "'+$specPath+'" --worker --pipe "'+$PipeName+'" --server-pid '+$ServerPID+' --server-birth '+$ServerBirth);
        expected_worker_executable=$ExpectedWorkerExecutable;working_directory=$repoRoot;
        runner_sha256=(Get-FileHash -LiteralPath $expectedRunner -Algorithm SHA256).Hash.ToLowerInvariant();
        spec_sha256=(Get-FileHash -LiteralPath $specPath -Algorithm SHA256).Hash.ToLowerInvariant();credential_in_task_or_args=$false}
    Write-Evidence $controlPath $record $true
    try {
        $action=New-ScheduledTaskAction -Execute $Interpreter -Argument $record.action_arguments -WorkingDirectory $repoRoot
        $principal=New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
        $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([timespan]::FromHours(12)) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $definition=New-ScheduledTask -Action $action -Principal $principal -Settings $settings -Description $record.ownership_marker
        Register-ScheduledTask -TaskName $record.task_name -TaskPath $record.task_path -InputObject $definition|Out-Null
        $record.created_by_this_runtime=$true;$record.cleanup_required=$true;$record.status='REGISTERED'
        $xml=[string](Export-ScheduledTask -TaskName $record.task_name -TaskPath $record.task_path)
        [IO.File]::WriteAllText((Join-Path $outputPath 'registered-task.xml'),$xml,[Text.UTF8Encoding]::new($false))
        $record.registered_xml_sha256=Digest $xml
        $record.start_requested_at_utc=[datetime]::UtcNow.ToString('o')
        Write-Evidence $controlPath $record
        Start-ScheduledTask -TaskName $record.task_name -TaskPath $record.task_path
        $record.status='START_REQUESTED'
    } catch {
        $record.status='FAILED_WITHOUT_RETRY';$record.exception_type=$_.Exception.GetType().FullName
    } finally {Write-Evidence $controlPath $record}
    $record|ConvertTo-Json -Depth 10
    exit
}

$record=Get-Content -LiteralPath $controlPath -Raw|ConvertFrom-Json
Owned-Record $record
if ($Mode -eq 'VerifyWorker') {
    $task=Get-ScheduledTask -TaskName $record.task_name -TaskPath $record.task_path
    $xml=[string](Export-ScheduledTask -TaskName $record.task_name -TaskPath $record.task_path)
    if ($task.State.ToString() -ne 'Running' -or $task.Description -cne $record.ownership_marker -or (Digest $xml) -cne $record.registered_xml_sha256 -or @($task.Actions).Count -ne 1) {throw 'Owned task is not running with the registered definition'}
    $action=$task.Actions[0]
    if ($action.Execute -cne $record.action_executable -or $action.Arguments -cne $record.action_arguments -or $action.WorkingDirectory -cne $record.working_directory) {throw 'Task action mismatch'}
    $worker=Get-Content -LiteralPath (Join-Path $outputPath 'worker.json') -Raw|ConvertFrom-Json
    $process=Get-CimInstance Win32_Process -Filter ('ProcessId = '+[int]$worker.pid)
    if (-not $process -or $process.ExecutablePath -ine $record.expected_worker_executable -or -not $process.CommandLine.EndsWith($record.action_arguments,[StringComparison]::Ordinal)) {throw 'Actual worker process differs from the task action'}
    $created=[datetimeoffset]$process.CreationDate
    if ($created -lt ([datetimeoffset]::Parse($record.start_requested_at_utc)).AddSeconds(-2) -or $created -gt [datetimeoffset]::UtcNow) {throw 'Worker OS creation time outside this launch'}
    $fixedRunner=Join-Path $PSScriptRoot 'runner.py'
    if ((Get-FileHash -LiteralPath $fixedRunner -Algorithm SHA256).Hash.ToLowerInvariant() -cne $record.runner_sha256) {throw 'Runtime source changed'}
    [ordered]@{verified=$true;pid=[int]$worker.pid;process_creation_at_utc=$created.ToUniversalTime().ToString('o');running_task_action_matches_os_command=$true;expected_executable_matches=$true;runner_source_matches=$true}|ConvertTo-Json
    exit
}
$observation=Current-Observation $record
$stamp=[datetime]::UtcNow.ToString('yyyyMMdd-HHmmss-fffffff')
Write-Evidence (Join-Path $outputPath ('external-observation-'+$stamp+'.json')) $observation $true
if ($Mode -eq 'Observe') {$observation|ConvertTo-Json -Depth 8;exit}
if (-not $observation.identity_verified -or -not $observation.current_start_verified -or -not $observation.stopped) {throw 'Cleanup requires the exact owned stopped task and current start'}
$again=Current-Observation $record
if (-not $again.identity_verified -or -not $again.current_start_verified -or -not $again.stopped) {throw 'Cleanup recheck failed; no task is stopped or removed'}
Unregister-ScheduledTask -TaskName $record.task_name -TaskPath $record.task_path -Confirm:$false
if (@(Get-ScheduledTask -TaskName $record.task_name -TaskPath $record.task_path -ErrorAction SilentlyContinue).Count -ne 0) {throw 'Task removal was not confirmed'}
$cleanup=[ordered]@{schema_version='dive-owned-validation-task-cleanup-v1';at_utc=[datetime]::UtcNow.ToString('o');task_name=$record.task_name;task_path=$record.task_path;
    deleted_only_this_created_task=$true;exact_task_absent_after_cleanup=$true;exit_code=$again.exit_code;xml_sha256=$again.xml_sha256;
    worker_terminal_exists=$again.worker_terminal_exists;observer_wrote_worker_terminal=$false;automatic_retry=$false;semantic_result='NOT_ESTABLISHED_BY_PROCESS_EXIT'}
Write-Evidence (Join-Path $outputPath 'cleanup.json') $cleanup $true
$cleanup|ConvertTo-Json -Depth 8

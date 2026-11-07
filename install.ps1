# agent-pathfinder (sw) install: link the skills into one host's root, put `sw` on PATH, self-check.
#
#   pwsh -File install.ps1 -SkillsFor zcode             link the skills into ~/.agents/skills (what ZCode reads)
#   pwsh -File install.ps1 -SkillsFor none              no links at all: PATH and the language record only
#   pwsh -File install.ps1 -SkillsFor zcode -Lang zh    install the Chinese skills (the default is en)
#   pwsh -File install.ps1 -SkillsFor zcode -Check      print every link, PATH and record change, write nothing
#   pwsh -File install.ps1 -SkillsFor zcode -TakeShared take over ~/.agents/skills/shared (a link elsewhere)
#   pwsh -File install.ps1 -SkillsFor zcode -Uninstall  remove what this checkout linked in
#
# `-SkillsFor` has no default, because a skill root belongs to a host and this script may not guess which
# one you meant: without it the three choices are printed and nothing is touched. `-SkillsFor dsh` is
# refused -- the DSH plugin installs its own skills, in DSH's shape, into `~/.dsh/skills`; the skills
# in this checkout are the command-line ones and read as wrong there. `-SkillsFor zcode` links into
# `~/.agents/skills`: that is the root ZCode reads, and DSH also scans it, at a rank below its own
# (see the line this script prints when it finds DSH on the machine).
#
# The skills come in one language per install: what gets linked is `skills\<lang>\sw-*`, because a
# method name has exactly one skill (`~/.agents/skills/<method>/SKILL.md`). A skill a plugin ships is
# not in the core's skills tree: it lives in the plugin's own folder
# (`plugins\<name>\skills\<lang>\<method>`) and is linked from there, so removing the plugin folder
# takes its skills with it. A method the chosen language has no skill for is borrowed from another
# language's folder and the run says which one; a method no language ships is said missing, never
# skipped in silence -- the link is what a session reads, so a language choice may not quietly delete
# it.
# `-Lang` beats the choice recorded in `~/.superworker/lang.txt`, which beats the default; the record is
# what keeps a re-run -- the repair -- from silently switching the language a user picked.
#
# Skills are linked as junctions: editing a file in this repository takes effect at once, with
# nothing to sync. `sw` sits on PATH exactly once: a re-run first removes another checkout's
# `tools\bin` (recognized by its own signature and the one two levels up), then appends its own --
# it resolves the tool tree's location by itself, so the repository can move anywhere; move it,
# re-run this script. That appended entry is also recorded in ~/.superworker/install.txt: when the old
# directory is off the disk, the record is what gets removed. -Uninstall leaves PATH alone
# (something else may still be using it): after you clear PATH, re-run this script once and the
# recorded entry is swapped out.
#
# The skill root is never a project's: a project's `.agents/skills` is the project's own to commit,
# and this script only writes the host roots above.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('zcode', 'dsh', 'none')]
    [string]$SkillsFor,
    [string]$Lang,
    [switch]$TakeShared,
    [switch]$Uninstall,
    [switch]$SkipPath,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$binDir = Join-Path $repo 'tools\bin'
$swPy = Join-Path $binDir 'sw.py'
$skillsDir = Join-Path $repo 'skills'
# What a fresh install gets when neither -Lang nor the record says.
$defaultLang = 'en'
$skillsRoot = Join-Path $HOME '.agents\skills'
$sharedRoot = Join-Path $skillsRoot 'shared'
$sharedSw = Join-Path $sharedRoot 'superworker'
$markerFile = Join-Path $HOME '.superworker\install.txt'
$langFile = Join-Path $HOME '.superworker\lang.txt'
# The DSH side's installed skill set: the plugin maintains it, and this script only reads it to say
# what sits above the root it links into.
$dshManifest = Join-Path $HOME '.dsh\superworker\skills.json'

function Say([string]$what, [string]$detail = '') { Write-Host ("  {0,-16}{1}" -f $what, $detail) }

# `-Check` is this script's own spelling for a dry run, and `-WhatIf` (the binding's) is the same
# answer: every action below goes through Act, so the two can never print a plan the other would not
# carry out.
$dry = [bool]$Check -or $WhatIfPreference

function Act([string]$what, [string]$detail, [scriptblock]$action) {
    if ($dry) { Say 'would be' "$what $detail"; return }
    & $action
    Say $what $detail
}

function Get-SkillLangs([string]$checkout) {
    # What a checkout offers: `skills\<lang>\shared\superworker`, one level down. This is also the
    # signature that tells an agent-pathfinder checkout from any other directory that has a `bin`.
    $dir = Join-Path $checkout 'skills'
    if (-not (Test-Path -LiteralPath $dir)) { return @() }
    return @(Get-ChildItem -LiteralPath $dir -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'shared\superworker') } | ForEach-Object { $_.Name })
}

function Test-Reparse([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    return ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

function Test-SwBin([string]$dir) {
    # An agent-pathfinder checkout's `tools\bin`: the two signatures are `sw.py` and a skill tree
    # two levels up. Recognizing it is what lets another checkout's PATH entry be removed.
    if (-not $dir) { return $false }
    $checkout = Split-Path -Parent (Split-Path -Parent $dir)
    if (-not $checkout) { return $false }
    return (Test-Path -LiteralPath (Join-Path $dir 'sw.py')) -and (@(Get-SkillLangs $checkout).Count -gt 0)
}

function New-Junction([string]$link, [string]$target) {
    if (Test-Reparse $link) { cmd /c rmdir "$link" | Out-Null }
    elseif (Test-Path -LiteralPath $link) {
        throw "$link already exists and is a real directory. Check inside it, delete it, re-run."
    }
    cmd /c mklink /J "$link" "$target" | Out-Null
    if (-not (Test-Reparse $link)) { throw "the junction was not made: $link" }
}

# ── 0. which host's skill root ───────────────────────────────────────────────
if (-not $SkillsFor) {
    Write-Host 'Name the host whose skill root this install links into:'
    Write-Host '  -SkillsFor zcode    link into ~\.agents\skills (what ZCode reads)'
    Write-Host '  -SkillsFor none     no links: PATH and the language record only'
    Write-Host '  -SkillsFor dsh      not this script''s: the DSH plugin installs those skills itself'
    Write-Host ''
    Write-Host 'Nothing was touched.'
    exit 2
}
if ($SkillsFor -eq 'dsh') {
    Write-Host '-SkillsFor dsh is refused on purpose: the DSH side installs its own skills --'
    Write-Host 'the plugin writes DSH-shaped ones into ~\.dsh\skills, and the command lines this'
    Write-Host 'checkout''s skills carry would be wrong there. Pick -SkillsFor zcode (ZCode) or -SkillsFor none.'
    exit 2
}
$linkSkills = ($SkillsFor -eq 'zcode')

if (-not $dry -and -not (Test-Path -LiteralPath $swPy)) { throw "cannot find $swPy -- run this from the repository root." }

if ($linkSkills) {
    $skillLangs = @(Get-SkillLangs $repo)
    if (-not $Lang) {
        $Lang = if (Test-Path -LiteralPath $langFile) { (Get-Content -LiteralPath $langFile -Raw -Encoding UTF8).Trim() } else { $defaultLang }
    }
    if ($skillLangs -notcontains $Lang) {
        throw "no skills in language '$Lang' here (this checkout has: $($skillLangs -join ', '))"
    }
    $langSkills = Join-Path $skillsDir $Lang
    $repoSharedSw = Join-Path $langSkills 'shared\superworker'
    if (-not (Test-Path -LiteralPath $repoSharedSw)) { throw "cannot find $repoSharedSw -- run this from the repository root." }
    if (-not (Test-Path -LiteralPath $skillsRoot)) { New-Item -ItemType Directory -Force -Path $skillsRoot | Out-Null }
}

# The two skill sets share one namespace: a name under `$skillsRoot`. They are gathered here, before
# section 1 writes a junction, because that is the only moment both are in hand with nothing on disk
# yet. A plugin skill, or a method a node's declaration names, that carries a core skill's name would
# take that junction in section 2: the core skill left unreachable under its own name, the run still
# reporting success, and every re-run repeating the theft. So a name both sides carry is refused
# before the first link is made or removed; a name only one side carries is an ordinary link, and a
# plugin method that merely starts with `sw-` is a method like any other.
$skills = @()
if ($linkSkills) {
    $skills = Get-ChildItem -LiteralPath $langSkills -Directory | Where-Object { $_.Name -like 'sw-*' }
}
$pluginsRoot = Join-Path $repo 'plugins'

function Get-SkillFolders([string]$pluginDir) {
    # Every skill one plugin ships: `skills\<lang>\<method>`, in language order. Which of them gets
    # linked is decided below, not here: a filter here is what would silently turn a missing language
    # into a missing link.
    $root = Join-Path $pluginDir 'skills'
    $found = @()
    if (-not (Test-Path -LiteralPath $root)) { return $found }
    foreach ($langDir in Get-ChildItem -LiteralPath $root -Directory) {
        if ($langDir.Name -like '.*' -or $langDir.Name -like '__*') { continue }
        foreach ($method in Get-ChildItem -LiteralPath $langDir.FullName -Directory) {
            $found += [pscustomobject]@{ Method = $method.Name; Folder = $method.FullName
                                         Plugin = (Split-Path -Leaf $pluginDir)
                                         Language = $langDir.Name }
        }
    }
    return $found
}

function Get-DeclaredMethods([string]$pluginDir) {
    # The methods a declaration points a node at (`metrics.<key>.method`): the names the install owes a
    # readable skill, each with the plugin that declared it, so a name two owners share can say who
    # claims it. Only the caller says what to do when no skill answers one.
    $file = Join-Path $pluginDir 'plugin.json'
    if (-not (Test-Path -LiteralPath $file)) { return @() }
    try { $decl = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return @() }
    $found = @()
    if ($decl.metrics) {
        foreach ($metric in $decl.metrics.PSObject.Properties) {
            $method = $metric.Value.method
            if ($method -is [string] -and $method) {
                $found += [pscustomobject]@{ Method = $method; Plugin = (Split-Path -Leaf $pluginDir) }
            }
        }
    }
    return $found
}

$pluginFolders = @()
if (Test-Path -LiteralPath $pluginsRoot) {
    $pluginFolders = @(Get-ChildItem -LiteralPath $pluginsRoot -Directory | Where-Object {
        $_.Name -notlike '.*' -and $_.Name -notlike '__*' })
}
$allSkills = @()
$declared = @()
foreach ($plugin in $pluginFolders) {
    $allSkills += Get-SkillFolders $plugin.FullName
    $declared += Get-DeclaredMethods $plugin.FullName
}

# Who claims a name, the first claimant winning: the throw can name one plugin, and the one whose
# claim the run would have read first is the one to send the reader to.
$claims = @{}
foreach ($skill in $allSkills) {
    $key = $skill.Method.ToLowerInvariant()
    if (-not $claims.ContainsKey($key)) { $claims[$key] = $skill.Plugin }
}
foreach ($decl in $declared) {
    $key = $decl.Method.ToLowerInvariant()
    if (-not $claims.ContainsKey($key)) { $claims[$key] = $decl.Plugin }
}
# Uninstall is not refused: it writes no junction, and taking a stolen one off is how the core skill
# gets its own name back for the next run.
if (-not $Uninstall) {
    foreach ($skill in $skills) {
        $key = $skill.Name.ToLowerInvariant()
        if ($claims.ContainsKey($key)) {
            throw ("plugin '$($claims[$key])' claims the skill name '$($skill.Name)', which belongs to a " +
                   "core skill here: one method name has one skill, so that plugin's link would overwrite " +
                   "the core's. Rename the plugin's method or the core skill, and re-run.")
        }
    }
}

if ($linkSkills -and -not $Uninstall -and (Test-Path -LiteralPath $dshManifest)) {
    # Two implementations of one record layer, two skill sets: the plugin's lives in `~/.dsh/skills`
    # (rank 400) and this one goes into the shared root (500), so a same-named skill reads the DSH
    # copy. Saying so is what keeps a person from editing the wrong file.
    try {
        $manifest = Get-Content -LiteralPath $dshManifest -Raw -Encoding UTF8 | ConvertFrom-Json
        $dshNames = @($manifest.skills | ForEach-Object { [string]$_.name })
        Say 'note' ("DSH ships " + $dshNames.Count + " skills of its own in " + $manifest.root +
            " (by plugin; rank 400, above this root's 500)")
    } catch {
        Say 'warning' "could not read $dshManifest -- DSH's own skill set was not compared"
    }
}

# ── 1. skill junctions ────────────────────────────────────────────────────────
foreach ($skill in $skills) {
    $link = Join-Path $skillsRoot $skill.Name
    if ($Uninstall) {
        if (Test-Reparse $link) { Act 'removed' $skill.Name { cmd /c rmdir "$link" | Out-Null } }
    } else {
        Act 'linked' "$($skill.Name)  ->  $($skill.FullName)" { New-Junction $link $skill.FullName }
    }
}
if ($linkSkills -and -not $Uninstall) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $langFile) | Out-Null
    Act 'recorded' "language $Lang in $langFile" { Set-Content -LiteralPath $langFile -Value $Lang -Encoding UTF8 }
}

# ── 2. plugin skills: a skill a plugin ships lives in the plugin's own folder ──
# `<plugin>/skills/<lang>/<method>` is where a method's skill is; this step is what turns one of those
# folders into the `~/.agents/skills/<method>` path a session reads. The install's language is the
# first pick, and a method it has no folder for is taken from another language's folder -- a skill in
# the wrong language is readable, a link that is not there is not -- with one line saying which
# language it came from. A method no language ships at all is said missing for the same reason: the
# run may not report success while a node's method has nothing to read. Removing a plugin folder is
# how its skills go away: the link is dead from that moment on, and the next run of this script takes
# the dead link off -- that is all "installed and removed with it" means. Which links are this step's
# to reconcile is answered by the link's own shape (`Test-PluginSkill`), never by where the checkout
# sits at the moment: a repository that moves keeps its skill links attributable, and a repair then
# re-points them at the new place instead of leaving them dead behind it. The set itself is gathered
# above, before the core's links are made, so a name the two sides share is refused there instead of
# linked here.

function Test-PluginSkill([string]$target) {
    # Is this link pointing at a plugin's skill -- `<checkout>\plugins\<name>\skills\<lang>\<method>`?
    # The shape is the signature, the same way `Test-SwBin` recognizes a `tools\bin` by its own.
    # Nothing else makes a link of that shape, and one method name has exactly one skill, so the skill
    # links in the skills tree are this script's to keep in shape -- and recognizing them by shape is
    # what keeps them attributable once the repository has moved, where asking for this checkout's own
    # path would leave every one of them dead and nameless.
    if (-not $target) { return $false }
    $parts = @($target.TrimEnd('\') -split '\\')
    if ($parts.Count -lt 5) { return $false }
    $tail = $parts[($parts.Count - 5)..($parts.Count - 1)]
    return ($tail[0] -eq 'plugins') -and ($tail[2] -eq 'skills')
}

function Test-UnderPlugins([string]$target) {
    # A skill link pointing into THIS checkout's plugin tree: what `-Uninstall` takes off, because
    # uninstalling one checkout is not the moment to clear another one's links. The repair does not ask
    # this -- it reconciles every skill link there, whoever made it (see `Test-PluginSkill`).
    if (-not $target) { return $false }
    $root = $pluginsRoot.TrimEnd('\') + '\'
    return $target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
}

$pluginSkills = @()
if ($linkSkills) {
    $pluginSkills = @($allSkills | Where-Object { $_.Language -eq $Lang })
    $spareSkills = @{}
    foreach ($skill in $allSkills) {
        if ($skill.Language -eq $Lang) { continue }
        $key = $skill.Method.ToLowerInvariant()
        if (-not $spareSkills.ContainsKey($key)) { $spareSkills[$key] = $skill }
    }
    foreach ($key in @($spareSkills.Keys | Sort-Object)) {
        if (@($pluginSkills | Where-Object { $_.Method.ToLowerInvariant() -eq $key }).Count) { continue }
        $borrowed = $spareSkills[$key]
        $pluginSkills += $borrowed
        if (-not $Uninstall) {
            Say 'borrowed' ("$($borrowed.Method)  from $($borrowed.Language)  (this install's language, $Lang, ships no skill for it)")
        }
    }
    $shipped = @{}
    foreach ($skill in $allSkills) { $shipped[$skill.Method.ToLowerInvariant()] = $true }
    if (-not $Uninstall) {
        foreach ($method in @($declared.Method | Sort-Object -Unique)) {
            if ($shipped.ContainsKey($method.ToLowerInvariant())) { continue }
            Say 'missing' ("$method  (no language ships this skill; a node naming it would land on nothing)")
        }
    }
}

$wantedSkills = @{}
foreach ($skill in $pluginSkills) {
    if (-not $wantedSkills.ContainsKey($skill.Method.ToLowerInvariant())) {
        $wantedSkills[$skill.Method.ToLowerInvariant()] = $skill
    }
}

if ($linkSkills -and (Test-Path -LiteralPath $skillsRoot)) {
    foreach ($link in Get-ChildItem -LiteralPath $skillsRoot -Directory -Force) {
        if (-not (Test-Reparse $link.FullName)) { continue }
        $target = (Get-Item -LiteralPath $link.FullName -Force).Target
        if (-not (Test-PluginSkill $target)) { continue }
        if ($Uninstall) {
            if (-not (Test-UnderPlugins $target)) { continue }
            Act 'removed' $link.Name { cmd /c rmdir "$($link.FullName)" | Out-Null }
        } elseif (-not $wantedSkills.ContainsKey($link.Name.ToLowerInvariant())) {
            Act 'unlinked' "$($link.Name)  (its plugin is gone, or this run installs another language)" {
                cmd /c rmdir "$($link.FullName)" | Out-Null }
        }
    }
}

if ($linkSkills -and -not $Uninstall) {
    foreach ($skill in $pluginSkills) {
        $key = $skill.Method.ToLowerInvariant()
        if ($wantedSkills[$key].Folder -ne $skill.Folder) {
            Say 'skipped' "$($skill.Method)  (a second plugin, $($skill.Plugin), ships the same skill)"
            continue
        }
        Act 'linked' "$($skill.Method)  ->  $($skill.Folder)" {
            New-Junction (Join-Path $skillsRoot $skill.Method) $skill.Folder }
    }
}

# ── 3. shared/superworker: `../shared/superworker/…` in skill bodies has to land here ───────────
if ($linkSkills) {
    if ($Uninstall) {
        if (Test-Reparse $sharedSw) { Act 'removed' 'shared/superworker' { cmd /c rmdir "$sharedSw" | Out-Null } }
    } elseif (Test-Reparse $sharedRoot) {
        $target = (Get-Item -LiteralPath $sharedRoot -Force).Target
        if (-not $TakeShared) {
            Say 'skipped' "shared is one link pointing at $target"
            Write-Host '    → `../shared/superworker/…` in skill bodies would not land here. To make it a real directory'
            Write-Host '      (holding this repository''s superworker, plus what has to stay put), re-run with -TakeShared.'
        } else {
            $oldTarget = (Get-Item -LiteralPath $sharedRoot -Force).Target
            $keep = @()
            if ($oldTarget -and (Test-Path -LiteralPath $oldTarget)) {
                $keep = Get-ChildItem -LiteralPath $oldTarget -Force | Where-Object { $_.Name -ne 'superworker' }
            }
            Act 'taken over' "shared is now a real directory (it pointed at: $oldTarget)" {
                cmd /c rmdir "$sharedRoot" | Out-Null
                New-Item -ItemType Directory -Force -Path $sharedRoot | Out-Null
                foreach ($item in $keep) {
                    $link = Join-Path $sharedRoot $item.Name
                    if ($item.PSIsContainer) { New-Junction $link $item.FullName } else { Copy-Item -LiteralPath $item.FullName -Destination $link }
                }
                foreach ($item in $keep) { Say 'kept' "$($item.Name)  ->  $($item.FullName)" }
            }
        }
    } elseif (Test-Path -LiteralPath $sharedRoot) {
        Say 'already a dir' $sharedRoot
    } else {
        # Nothing owns `shared` here yet -- the first install on a machine, or a root someone cleaned.
        # It has to become a real directory: the branch below links this checkout's `superworker`
        # into it, and a skill body that says `../shared/superworker/…` reads a path that only exists
        # once it is one.
        Act 'created' $sharedRoot { New-Item -ItemType Directory -Force -Path $sharedRoot | Out-Null }
    }
    if (-not $Uninstall -and (Test-Path -LiteralPath $sharedRoot) -and -not (Test-Reparse $sharedRoot)) {
        Act 'linked' "shared/superworker  ->  $repoSharedSw" { New-Junction $sharedSw $repoSharedSw }
    }
}

# ── 4. PATH ───────────────────────────────────────────────────────────────────
if ($Uninstall) {
    Say 'note' 'PATH is left for you to clean (something else may still be using it)'
} elseif (-not $SkipPath) {
    # Read and write the value raw, through the key: GetEnvironmentVariable hands back the expanded
    # string and SetEnvironmentVariable writes REG_SZ, so a rewrite through them would bake
    # `%JAVA_HOME%\bin` into the literal path of the day. The stored kind is kept, so a reference
    # stays a reference.
    $envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $userPath = [string]$envKey.GetValue('Path', '',
                        [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = [Microsoft.Win32.RegistryValueKind]::String
        try { $kind = $envKey.GetValueKind('Path') } catch { }
        $parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
        $last = if (Test-Path -LiteralPath $markerFile) { (Get-Content -LiteralPath $markerFile -Raw -Encoding UTF8).Trim() } else { '' }
        $had = $parts -contains $binDir
        # The entry the last install added, and another checkout's bin: both come off, this one stays.
        $others = @($parts | Where-Object { $_ -ne $binDir -and ($_ -eq $last -or (Test-SwBin $_)) })
        $newParts = @($parts | Where-Object { $others -notcontains $_ })
        if (-not $had) { $newParts += $binDir }
        if ($others.Count) {
            if ($dry) { Say 'would be' ("removed " + ($others -join '; ')) }
            else { Say 'removed' ($others -join '; ') }
        }
        if ($had) {
            Say 'PATH' 'already in there'
        } elseif ($dry) {
            Say 'would be' "appended $binDir (open a new terminal to see it)"
        } else {
            Say 'PATH' "appended $binDir (open a new terminal to see it)"
        }
        if (-not $dry -and ($others.Count -or -not $had)) {
            $envKey.SetValue('Path', ($newParts -join ';'), $kind)
        }
    } finally {
        $envKey.Close()
    }
    Act 'recorded' "the PATH entry in $markerFile" {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $markerFile) | Out-Null
        Set-Content -LiteralPath $markerFile -Value $binDir -Encoding UTF8 }
}

# ── 5. self-check ───────────────────────────────────────────────────────────
# Whether the install holds is judged on two sides: this script's half (skill junctions, PATH)
# and the tool tree's half (tool files, the Python version, the ledger, the merge driver). `sw
# doctor` answers for the tool half, so it is asked once here; its red verdict is this script's
# exit code. It reads the layout by the project root it resolved itself (a worktree resolves
# to the main tree).
$verdict = 0
Write-Host ''
if ($linkSkills) {
    Say 'note' "skill language: $Lang"
    Say 'note' ("sw skills visible here: " + (@(Get-ChildItem -LiteralPath $skillsRoot -Directory | Where-Object {
        $_.Name -like 'sw-*' -and -not (Test-PluginSkill (Get-Item -LiteralPath $_.FullName -Force).Target) }).Count))
    $skillLinks = @(Get-ChildItem -LiteralPath $skillsRoot -Directory -Force -ErrorAction SilentlyContinue | Where-Object {
        (Test-Reparse $_.FullName) -and (Test-PluginSkill (Get-Item -LiteralPath $_.FullName -Force).Target) })
    $readableSkills = @($skillLinks | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') })
    Say 'note' ("plugin skills visible here: " + $readableSkills.Count)
    if ($readableSkills.Count -lt $skillLinks.Count) {
        # A link the repair left in place with nothing behind it: the method's folder is there and its
        # `SKILL.md` is not. That is what a session would land on, and the doctor says the same thing
        # from the declaration's side, so the count alone is not allowed to stand for "all good".
        $emptySkills = @($skillLinks | Where-Object {
            -not (Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md')) })
        Say 'warning' ("a skill link with nothing to read behind it: " + ($emptySkills.Name -join ', '))
    }
    if (Test-Path -LiteralPath $sharedSw) { Say 'note' 'shared/superworker is in place' } else { Say 'warning' 'shared/superworker is not in place yet (its own section above)' }
}
if (-not $Uninstall) {
    Write-Host ''
    if ($dry) {
        Say 'would run' 'sw doctor (the tool tree half)'
    } else {
        Push-Location -LiteralPath $repo
        try { & (Join-Path $binDir 'sw.cmd') doctor; $verdict = $LASTEXITCODE } finally { Pop-Location }
        if ($verdict -ne 0) { Write-Warning 'the tool tree half did not pass (see above): deal with it, then re-run this script.' }
    }
}
Write-Host "  Next: open a new terminal and run"
Write-Host "    sw version        # check that PATH is in effect"
Write-Host "    sw doctor         # health-check your project (the ledger, the merge driver)"
exit $verdict
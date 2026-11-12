<#
.SYNOPSIS
    Administración de áreas, canales y agentes del adaptador de Teams.

.DESCRIPTION
    Cargar las funciones en la sesión (requiere Azure CLI con sesión iniciada
    por una cuenta con el rol teams.areas.admin):

        . .\scripts\AgentesTeams.ps1

    Comandos:
        Get-AgentesEquipos   [-Equipo <texto>] [-IncluirPrivados]
                                                              Equipos y canales conocidos por el bot.
                                                              Los privados se omiten: el bot no funciona en ellos.
        Sync-AgentesEquipos  [-TeamId <id>]                   Vuelve a leer los canales desde Teams
        Get-AgentesAreas                                      Resumen de áreas
        Get-AgentesArea      -Area <id>                       Detalle de un área
        Add-AgentesCanal     -Area <id> -Alias <alias> -Canal <nombre> [-Equipo <nombre>]
        Remove-AgentesCanal  -Area <id> -Alias <alias>
        Set-AgentesAgente    -Area <id> -Alias <alias> [-TriggerId agtch_…] [-Nombre <texto>]
                             [-Descripcion <texto>] [-Grupos <nombres u Object IDs>] [-Modo any|all]
                             [-TokenSecret <secreto>]
        Remove-AgentesAgente -Area <id> -Alias <alias>
        Enable-AgentesArea / Disable-AgentesArea -Area <id>

    Los grupos se pueden indicar por nombre (r-grpAg-Meta); el script busca su
    Object ID en Entra. Variables opcionales: AGENTES_TEAMS_URL y
    AGENTES_TEAMS_AUDIENCE (por defecto, el ambiente DEV).

.EXAMPLE
    Get-AgentesEquipos -Equipo "Transformación"
    Add-AgentesCanal -Area transformacion-digital -Alias td_ventas -Canal "Ventas"
    Set-AgentesAgente -Area transformacion-digital -Alias metaads -TriggerId agtch_123 `
        -Nombre "Analista Meta Ads" -Grupos r-grpAg-Meta
#>

$global:AgentesTeamsUrl = if ($env:AGENTES_TEAMS_URL) { $env:AGENTES_TEAMS_URL } else { 'https://app-teams-adapter-dev-clc.azurewebsites.net' }
$global:AgentesTeamsAudience = if ($env:AGENTES_TEAMS_AUDIENCE) { $env:AGENTES_TEAMS_AUDIENCE } else { 'api://ffa8971e-6c21-4c1a-8dd7-f4f74e623ca7' }
$global:AgentesTeamsGuid = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

function Get-AgentesToken([string]$Resource) {
    $token = az account get-access-token --resource $Resource --query accessToken -o tsv 2>$null
    if (-not $token) {
        throw "No se obtuvo token para $Resource. Ejecuta 'az login' con tu cuenta de administrador."
    }
    return $token
}

function Invoke-Agentes {
    param([string]$Method, [string]$Path, $Body = $null)
    $headers = @{ Authorization = "Bearer $(Get-AgentesToken $global:AgentesTeamsAudience)" }
    $params = @{ Method = $Method; Uri = "$($global:AgentesTeamsUrl)$Path"; Headers = $headers }
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 10
        $params.ContentType = 'application/json; charset=utf-8'
        $params.Body = [Text.Encoding]::UTF8.GetBytes($json)
    }
    try {
        # Invoke-WebRequest + decodificación explícita: PowerShell 5.1 lee como
        # Latin-1 las respuestas JSON sin charset y rompe los acentos.
        $response = Invoke-WebRequest @params -UseBasicParsing
        $bytes = $response.RawContentStream.ToArray()
        if ($bytes.Length -eq 0) { return $null }
        return [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    } catch {
        $detail = $_.ErrorDetails.Message
        if ($detail) {
            try { $detail = ($detail | ConvertFrom-Json).error } catch { }
            throw "Error del adaptador: $detail"
        }
        throw
    }
}

function Resolve-AgentesGrupo([string]$Grupo) {
    if ($Grupo -match $global:AgentesTeamsGuid) { return $Grupo.ToLower() }
    $token = Get-AgentesToken 'https://graph.microsoft.com'
    $name = $Grupo.Replace("'", "''")
    $uri = "https://graph.microsoft.com/v1.0/groups?`$filter=displayName eq '$name'&`$select=id,displayName"
    $found = @((Invoke-RestMethod -Uri $uri -Headers @{ Authorization = "Bearer $token" }).value)
    if ($found.Count -ne 1) { throw "Se esperaba un único grupo llamado '$Grupo' y se encontraron $($found.Count)." }
    return $found[0].id
}

function Get-AgentesEquipos {
    param([string]$Equipo = '', [switch]$IncluirPrivados)
    $query = if ($IncluirPrivados) { '?includePrivate=true' } else { '' }
    $teams = (Invoke-Agentes GET "/api/admin/teams$query").teams
    foreach ($t in $teams) {
        if ($Equipo -and $t.teamName -notlike "*$Equipo*") { continue }
        foreach ($c in $t.channels) {
            [pscustomobject]@{
                Equipo         = $t.teamName
                Canal          = $c.channelName
                Tipo           = $c.channelType
                Area           = $c.areaId
                Alias          = $c.alias
                ConversationId = $c.conversationId
            }
        }
    }
}

function Sync-AgentesEquipos {
    param([string]$TeamId = '')
    $body = if ($TeamId) { @{ teamId = $TeamId } } else { @{} }
    (Invoke-Agentes POST '/api/admin/teams/sync' $body).teams |
        ForEach-Object { [pscustomobject]@{ Equipo = $_.teamName; Canales = $_.channels; Eliminados = $_.removed } }
}

function Get-AgentesAreas {
    (Invoke-Agentes GET '/api/admin/areas').areas | ForEach-Object {
        [pscustomobject]@{
            Area       = $_.id
            Nombre     = $_.displayName
            Habilitada = $_.enabled
            Canales    = ($_.channels.alias -join ', ')
            Agentes    = ($_.agents.alias -join ', ')
        }
    }
}

function Get-AgentesArea {
    param([Parameter(Mandatory)][string]$Area)
    Invoke-Agentes GET "/api/admin/areas/$Area"
}

function Add-AgentesCanal {
    param(
        [Parameter(Mandatory)][string]$Area,
        [Parameter(Mandatory)][string]$Alias,
        [Parameter(Mandatory)][string]$Canal,
        [string]$Equipo = ''
    )
    $found_channels = @(Get-AgentesEquipos -Equipo $Equipo | Where-Object { $_.Canal -eq $Canal })
    if ($found_channels.Count -eq 0) {
        throw "No encontré el canal '$Canal'. Revisa Get-AgentesEquipos o ejecuta Sync-AgentesEquipos."
    }
    if ($found_channels.Count -gt 1) {
        throw "Hay $($found_channels.Count) canales llamados '$Canal'. Indica -Equipo: " + (($found_channels.Equipo | Sort-Object -Unique) -join ', ')
    }
    $result = Invoke-Agentes PUT "/api/admin/areas/$Area/channels/$Alias" @{ conversationId = $found_channels[0].ConversationId; displayName = $Canal }
    "Canal '$Canal' ($($found_channels[0].Equipo)) agregado al área $Area como '$Alias'."
    $null = $result
}

function Remove-AgentesCanal {
    param([Parameter(Mandatory)][string]$Area, [Parameter(Mandatory)][string]$Alias)
    $null = Invoke-Agentes DELETE "/api/admin/areas/$Area/channels/$Alias"
    "Canal '$Alias' quitado del área $Area."
}

function Set-AgentesAgente {
    param(
        [Parameter(Mandatory)][string]$Area,
        [Parameter(Mandatory)][string]$Alias,
        [string]$TriggerId,
        [string]$Nombre,
        [string]$Descripcion,
        [string[]]$Grupos,
        [ValidateSet('any', 'all')][string]$Modo,
        [string]$TokenSecret
    )
    $body = @{}
    if ($PSBoundParameters.ContainsKey('TriggerId'))   { $body.triggerId = $TriggerId }
    if ($PSBoundParameters.ContainsKey('Nombre'))      { $body.displayName = $Nombre }
    if ($PSBoundParameters.ContainsKey('Descripcion')) { $body.description = $Descripcion }
    if ($PSBoundParameters.ContainsKey('Modo'))        { $body.accessMode = $Modo }
    if ($PSBoundParameters.ContainsKey('TokenSecret')) { $body.tokenSecret = $TokenSecret }
    if ($PSBoundParameters.ContainsKey('Grupos')) {
        $body.requiredGroups = @($Grupos | ForEach-Object { Resolve-AgentesGrupo $_ })
    }
    $result = Invoke-Agentes PUT "/api/admin/areas/$Area/agents/$Alias" $body
    $agent = $result.agents | Where-Object { $_.alias -eq $Alias.ToLower() }
    [pscustomobject]@{
        Area      = $Area
        Alias     = $agent.alias
        Nombre    = $agent.displayName
        TriggerId = $agent.triggerId
        Modo      = $agent.accessMode
        Grupos    = ($agent.requiredGroups -join ', ')
    }
}

function Remove-AgentesAgente {
    param([Parameter(Mandatory)][string]$Area, [Parameter(Mandatory)][string]$Alias)
    $null = Invoke-Agentes DELETE "/api/admin/areas/$Area/agents/$Alias"
    "Agente '$Alias' quitado del área $Area."
}

function Enable-AgentesArea {
    param([Parameter(Mandatory)][string]$Area)
    $null = Invoke-Agentes POST "/api/admin/areas/$Area/enable"
    "Área $Area habilitada."
}

function Disable-AgentesArea {
    param([Parameter(Mandatory)][string]$Area)
    $null = Invoke-Agentes POST "/api/admin/areas/$Area/disable"
    "Área $Area deshabilitada."
}

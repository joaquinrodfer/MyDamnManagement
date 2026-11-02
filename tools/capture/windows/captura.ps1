#Requires -Version 5.1
<#
.SYNOPSIS
    Apunta algo en MyDamnManagement desde Windows (P13).

.DESCRIPTION
    Abre un cuadro de texto (WinForms) con el portapapeles como valor inicial y
    manda lo escrito a POST /capture. El atajo global lo pone Windows, no este
    script: ver LEEME.md (acceso directo + PowerToys → Teclado).

    Variables de entorno:
      MDM_TOKEN  obligatorio. Token de captura: panel → menú del usuario →
                 «Tokens de captura» → Crear token (se enseña una sola vez).
                 Revocado, deja de valer al instante.
      MDM_URL    opcional. Por defecto http://127.0.0.1:8000

.PARAMETER Text
    Texto ya escrito. Si llega, no se abre el cuadro: sirve para encadenarlo
    desde otro script o desde una tecla de PowerToys que pase el texto
    seleccionado.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\captura.ps1 "Comprar pan"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Text
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Show-Aviso {
    <#
        Aviso al usuario. Un fallo no se puede quedar en una consola que no
        existe: cuando esto lo lanza un atajo, la ventana está oculta.
        -Grave abre un cuadro de error (hay que leerlo); sin -Grave, un globo
        de la bandeja que se va solo.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Mensaje,
        [switch]$Grave
    )
    if ($Grave) {
        [void][System.Windows.Forms.MessageBox]::Show(
            $Mensaje,
            'MyDamnManagement',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        )
        return
    }
    $globo = New-Object System.Windows.Forms.NotifyIcon
    try {
        $globo.Icon = [System.Drawing.SystemIcons]::Information
        $globo.Visible = $true
        $globo.ShowBalloonTip(3000, 'MyDamnManagement', $Mensaje,
            [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Milliseconds 3200
    } finally {
        $globo.Visible = $false
        $globo.Dispose()
    }
}

$url = $env:MDM_URL
if ([string]::IsNullOrWhiteSpace($url)) { $url = 'http://127.0.0.1:8000' }
$url = $url.TrimEnd('/')

$token = $env:MDM_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    Show-Aviso -Grave -Mensaje @'
Falta la variable de entorno MDM_TOKEN.

Crea un token en el panel (menú del usuario → «Tokens de captura» → Crear
token) y guárdalo como variable de usuario:

  setx MDM_TOKEN "el-token-que-te-enseñó"

Después cierra y vuelve a abrir la sesión (o el acceso directo), porque
setx no cambia el entorno de lo que ya está abierto.
'@
    exit 1
}

# ------------------------------------------------------------------- el texto
$texto = ''
if (-not [string]::IsNullOrWhiteSpace($Text)) {
    $texto = $Text
} else {
    # El portapapeles como valor inicial: lo normal es capturar algo que acabas
    # de copiar. Si otro programa lo tiene bloqueado, se sigue con vacío.
    $inicial = ''
    try {
        if ([System.Windows.Forms.Clipboard]::ContainsText()) {
            $inicial = [System.Windows.Forms.Clipboard]::GetText()
        }
    } catch {
        $inicial = ''
    }

    $formulario = New-Object System.Windows.Forms.Form
    $formulario.Text = 'Capturar en MyDamnManagement'
    $formulario.ClientSize = New-Object System.Drawing.Size(520, 300)
    $formulario.StartPosition = 'CenterScreen'
    $formulario.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $formulario.MaximizeBox = $false
    $formulario.MinimizeBox = $false
    $formulario.TopMost = $true

    $etiqueta = New-Object System.Windows.Forms.Label
    $etiqueta.Text = '¿Qué apuntas?  (Ctrl+Intro guarda, Esc cancela)'
    $etiqueta.AutoSize = $true
    $etiqueta.Location = New-Object System.Drawing.Point(12, 12)

    $cuadro = New-Object System.Windows.Forms.TextBox
    $cuadro.Multiline = $true
    $cuadro.AcceptsReturn = $true
    $cuadro.ScrollBars = [System.Windows.Forms.ScrollBars]::Vertical
    $cuadro.Location = New-Object System.Drawing.Point(12, 36)
    $cuadro.Size = New-Object System.Drawing.Size(496, 200)
    $cuadro.Font = New-Object System.Drawing.Font('Segoe UI', 10)
    $cuadro.Text = $inicial

    $guardar = New-Object System.Windows.Forms.Button
    $guardar.Text = 'Guardar'
    $guardar.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $guardar.Location = New-Object System.Drawing.Point(352, 250)
    $guardar.Size = New-Object System.Drawing.Size(75, 28)

    $cancelar = New-Object System.Windows.Forms.Button
    $cancelar.Text = 'Cancelar'
    $cancelar.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $cancelar.Location = New-Object System.Drawing.Point(433, 250)
    $cancelar.Size = New-Object System.Drawing.Size(75, 28)

    # Ctrl+Intro guarda sin tener que soltar el teclado: en un cuadro
    # multilínea, Intro a secas escribe un salto de línea (que es lo que se
    # espera al apuntar varias cosas).
    $cuadro.Add_KeyDown({
        param($emisor, $evento)
        if ($evento.Control -and $evento.KeyCode -eq [System.Windows.Forms.Keys]::Enter) {
            $evento.SuppressKeyPress = $true
            $script:formulario.DialogResult = [System.Windows.Forms.DialogResult]::OK
        }
    })

    $formulario.Controls.AddRange(@($etiqueta, $cuadro, $guardar, $cancelar))
    $formulario.AcceptButton = $guardar
    $formulario.CancelButton = $cancelar
    $formulario.Add_Shown({
        $script:cuadro.Focus()
        $script:cuadro.SelectionStart = $script:cuadro.Text.Length
    })

    $resultado = $formulario.ShowDialog()
    $texto = $cuadro.Text
    $formulario.Dispose()

    if ($resultado -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }  # cancelar no es un error
}

$texto = $texto.Trim()
if ([string]::IsNullOrWhiteSpace($texto)) { exit 0 }  # nada que apuntar: silencio

# ------------------------------------------------------------------- el POST
# El cuerpo va en UTF-8 a mano: Invoke-RestMethod con un `string` en el -Body
# manda la codificación de la consola y los acentos llegan rotos a la nota.
$cuerpo = @{ text = $texto; source = 'shortcut' } | ConvertTo-Json -Compress
$bytes = [System.Text.Encoding]::UTF8.GetBytes($cuerpo)

try {
    $respuesta = Invoke-RestMethod -Uri "$url/capture" `
        -Method Post `
        -Headers @{ Authorization = "Bearer $token" } `
        -ContentType 'application/json; charset=utf-8' `
        -Body $bytes `
        -TimeoutSec 15
} catch {
    # El mensaje propio de PowerShell («Unable to connect…») tapa el del
    # servidor, que es el que dice qué pasa (token revocado, texto demasiado
    # largo…). Se prefiere el `detail` de la respuesta cuando lo hay.
    $detalle = $_.Exception.Message
    $cuerpoError = $null
    if ($null -ne $_.ErrorDetails) { $cuerpoError = $_.ErrorDetails.Message }
    if (-not [string]::IsNullOrWhiteSpace($cuerpoError)) {
        $detalle = $cuerpoError
        try {
            $json = $cuerpoError | ConvertFrom-Json
            if ($null -ne $json.detail) { $detalle = $json.detail }
        } catch {
            # La respuesta de error no era JSON: se enseña tal cual.
        }
    }
    Show-Aviso -Grave -Mensaje "No se pudo guardar la captura.`r`n`r`n$detalle"
    exit 1
}

if ($null -ne $respuesta -and -not [string]::IsNullOrWhiteSpace($respuesta.title)) {
    Show-Aviso -Mensaje "Capturado: $($respuesta.title)"
} else {
    Show-Aviso -Mensaje 'Capturado.'
}
exit 0

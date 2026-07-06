const express = require('express')
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys')
const pino = require('pino')
const QRCode = require('qrcode')
const https = require('https')
const http = require('http')

const app = express()
app.use(express.json())

const PORT = process.env.PORT || 3000
const AUTH_DIR = '/app/wa_auth'

const TARGET_GROUPS = [
    'PREMIUM GOLD GROUP',
    'GOLD | BITCOIN | SIGNALS GROUP',
    'WINNERS GOLD SIGNAL',
    "Kevin's GOLD & BTC SIGNALS",
    'Dummy group testing'
]

// Hardcoded JIDs — announcement channels for communities, direct JID for regular groups
const HARDCODED_JIDS = {
    'PREMIUM GOLD GROUP':           '120363414747612793@g.us', // announcement channel
    'WINNERS GOLD SIGNAL':          '120363393171612639@g.us', // announcement channel
    'GOLD | BITCOIN | SIGNALS GROUP': '120363406855804020@g.us', // regular group
    "Kevin's GOLD & BTC SIGNALS":   '120363401990805201@g.us'  // regular group
}

let sock = null
let qrCode = null
let isConnected = false
let groupJids = {}

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    const { version } = await fetchLatestBaileysVersion()

    sock = makeWASocket({
        version,
        logger: pino({ level: 'silent' }),
        printQRInTerminal: true,
        auth: state,
        browser: ['RayGoldSignals', 'Chrome', '1.0.0'],
    })

    sock.ev.on('creds.update', saveCreds)

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            console.log('📱 QR code generated — scan it!')
            qrCode = await QRCode.toDataURL(qr)
            isConnected = false
        }

        if (connection === 'close') {
            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut
            console.log('❌ Connection closed. Reconnecting:', shouldReconnect)
            isConnected = false
            qrCode = null
            if (shouldReconnect) setTimeout(connectToWhatsApp, 3000)
        }

        if (connection === 'open') {
            console.log('✅ WhatsApp connected!')
            isConnected = true
            qrCode = null
            await findTargetGroups()
        }
    })
}

async function findTargetGroups() {
    groupJids = {}

    // Apply hardcoded JIDs first for community announcement groups
    for (const [name, jid] of Object.entries(HARDCODED_JIDS)) {
        groupJids[name] = jid
        console.log(`📌 Hardcoded: "${name}" (${jid})`)
    }

    // Scan for remaining groups not in hardcoded list
    try {
        const groups = await sock.groupFetchAllParticipating()
        for (const [jid, group] of Object.entries(groups)) {
            const name = group.subject?.trim()
            if (TARGET_GROUPS.includes(name) && !groupJids[name]) {
                groupJids[name] = jid
                console.log(`✅ Found by scan: "${name}" (${jid})`)
            }
        }
    } catch (err) {
        console.error('❌ Error scanning groups:', err.message)
    }

    const missing = TARGET_GROUPS.filter(g => !groupJids[g])
    if (missing.length > 0) console.log(`⚠️ Not found: ${missing.join(', ')}`)
}

async function fetchMediaBuffer(url) {
    return new Promise((resolve, reject) => {
        const client = url.startsWith('https') ? https : http
        client.get(url, (res) => {
            if (res.statusCode && res.statusCode >= 400) {
                reject(new Error(`HTTP ${res.statusCode} fetching ${url}`))
                return
            }
            const chunks = []
            res.on('data', chunk => chunks.push(chunk))
            res.on('end', () => resolve(Buffer.concat(chunks)))
            res.on('error', reject)
        }).on('error', reject)
    })
}

// ─────────────────────────────────────────────
// ROUTES
// ─────────────────────────────────────────────

app.get('/qr', async (req, res) => {
    if (isConnected) {
        const foundGroups = Object.entries(groupJids).map(([name, jid]) => `${name}: ${jid}`).join('<br>')
        const missing = TARGET_GROUPS.filter(g => !groupJids[g])
        return res.send(`
            <html><body style="font-family:Arial;text-align:center;padding:50px">
            <h2>✅ WhatsApp Connected!</h2>
            <h3>Groups found:</h3>
            <p>${foundGroups || 'Still searching...'}</p>
            ${missing.length ? `<h3>⚠️ Not found:</h3><p>${missing.join(', ')}</p>` : ''}
            </body></html>
        `)
    }
    if (qrCode) {
        return res.send(`
            <html><body style="font-family:Arial;text-align:center;padding:50px">
            <h2>📱 Scan QR Code with WhatsApp</h2>
            <p>Open WhatsApp → Settings → Linked Devices → Link a Device</p>
            <img src="${qrCode}" style="width:300px;height:300px"/>
            <p>Refresh this page after scanning</p>
            </body></html>
        `)
    }
    return res.send(`
        <html><body style="font-family:Arial;text-align:center;padding:50px">
        <h2>⏳ Generating QR code...</h2>
        <p>Please wait a few seconds and refresh</p>
        </body></html>
    `)
})

app.post('/send', async (req, res) => {
    const { message, group, image_url, video_url, voice_url, image_data } = req.body
    if (!message && !image_data && !image_url && !video_url && !voice_url) return res.status(400).json({ error: 'no message or media' })
    if (!isConnected) return res.status(503).json({ error: 'WhatsApp not connected' })

    if (Object.keys(groupJids).length === 0) await findTargetGroups()

    const targets = group
        ? Object.entries(groupJids).filter(([name]) => name === group)
        : Object.entries(groupJids)

    if (group && targets.length === 0) {
        console.log(`⚠️ Group not found: "${group}"`)
        return res.status(404).json({ error: `group not found: ${group}` })
    }

    // Accept image as base64 (preferred — no URL fetch needed) or as URL
    let imageBuffer = null
    if (image_data) {
        try {
            imageBuffer = Buffer.from(image_data, 'base64')
            console.log(`📷 Image from base64: ${imageBuffer.length} bytes`)
        } catch (err) {
            console.log(`⚠️ Could not decode base64 image: ${err.message}`)
        }
    } else if (image_url) {
        try {
            imageBuffer = await fetchMediaBuffer(image_url)
            console.log(`📷 Image fetched: ${image_url} (${imageBuffer.length} bytes)`)
        } catch (err) {
            console.log(`⚠️ Could not fetch image, sending text only: ${err.message}`)
        }
    }

    // Fetch video if provided (takes priority over image if both somehow given)
    let videoBuffer = null
    if (video_url) {
        try {
            videoBuffer = await fetchMediaBuffer(video_url)
            console.log(`🎥 Video fetched: ${video_url} (${videoBuffer.length} bytes)`)
        } catch (err) {
            console.log(`⚠️ Could not fetch video, falling back: ${err.message}`)
        }
    }

    // Fetch voice note if provided
    let voiceBuffer = null
    if (voice_url) {
        try {
            voiceBuffer = await fetchMediaBuffer(voice_url)
            console.log(`🎙️ Voice fetched: ${voice_url} (${voiceBuffer.length} bytes)`)
        } catch (err) {
            console.log(`⚠️ Could not fetch voice, falling back: ${err.message}`)
        }
    }

    const results = {}
    for (const [name, jid] of targets) {
        try {
            if (voiceBuffer) {
                // ptt:true renders as a proper WhatsApp voice note (blue mic bubble)
                await sock.sendMessage(jid, { audio: voiceBuffer, ptt: true, mimetype: 'audio/ogg; codecs=opus' })
                if (message && message.trim()) {
                    await sock.sendMessage(jid, { text: message })
                }
            } else if (videoBuffer) {
                await sock.sendMessage(jid, { video: videoBuffer, caption: message })
            } else if (imageBuffer) {
                await sock.sendMessage(jid, { image: imageBuffer, caption: message })
            } else {
                await sock.sendMessage(jid, { text: message })
            }
            results[name] = 'sent ✅'
            console.log(`✅ Sent to: ${name}`)
        } catch (err) {
            results[name] = `failed ❌: ${err.message}`
            console.error(`❌ Failed to send to ${name}:`, err.message)
        }
    }

    return res.json({ status: 'ok', results })
})

app.get('/status', (req, res) => {
    res.json({
        connected: isConnected,
        groups: groupJids,
        missing: TARGET_GROUPS.filter(g => !groupJids[g])
    })
})

app.get('/list-groups', async (req, res) => {
    try {
        const groups = await sock.groupFetchAllParticipating()
        const list = Object.values(groups).map(g => g.subject)
        res.json({ total: list.length, groups: list })
    } catch (err) {
        res.status(500).json({ error: err.message })
    }
})

app.get('/list-groups-full', async (req, res) => {
    try {
        const groups = await sock.groupFetchAllParticipating()
        const list = Object.entries(groups).map(([jid, g]) => ({
            jid,
            name: g.subject,
            isCommunity: g.isCommunity,
            isCommunityAnnounce: g.isCommunityAnnounce,
            linkedParent: g.linkedParent
        }))
        res.json({ total: list.length, groups: list })
    } catch (err) {
        res.status(500).json({ error: err.message })
    }
})

app.get('/refresh-groups', async (req, res) => {
    await findTargetGroups()
    res.json({ groups: groupJids, missing: TARGET_GROUPS.filter(g => !groupJids[g]) })
})

app.get('/', (req, res) => {
    res.json({ status: 'RayWhatsApp running ✅', connected: isConnected, groups: Object.keys(groupJids) })
})

// ─────────────────────────────────────────────
// START
// ─────────────────────────────────────────────

app.listen(PORT, () => {
    console.log(`🚀 RayWhatsApp server running on port ${PORT}`)
    connectToWhatsApp()
})

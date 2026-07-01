const express = require('express');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

let pgPool = null;
try {
    const { Pool } = require('pg');
    pgPool = Pool;
} catch (_) {
    console.warn('pg is not installed; foam metrics are disabled.');
}

let chokidar = null;
try {
    chokidar = require('chokidar');
} catch (error) {
    console.warn('chokidar is not installed; directory watch logs are disabled.');
}

const app = express();
const PORT = Number(process.env.PORT || 4000);

const PUBLIC_DIR = path.join(__dirname, 'public');

const DATA_DIR = path.resolve(process.env.DATA_DIR || path.join(__dirname, 'data'));
const HLS_OUTPUT_DIR = path.resolve(
    process.env.HLS_OUTPUT_DIR || path.join(__dirname, '../../hls_output')
);
const CONFIG_FILE = path.resolve(
    process.env.CONFIG_FILE || path.join(__dirname, '../../config.json')
);
const CAMERA_FOLDER_PREFIX = process.env.CAMERA_FOLDER_PREFIX || 'camera_';
const CONFIGS_FILE = path.join(DATA_DIR, 'configs.json');
const MAX_CONFIGS = 7;

function loadStreamStaleSec() {
    if (process.env.STREAM_STALE_SEC) {
        const parsed = Number(process.env.STREAM_STALE_SEC);
        if (!Number.isNaN(parsed) && parsed > 0) return parsed;
    }
    if (CONFIG_FILE && fs.existsSync(CONFIG_FILE)) {
        try {
            const config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
            if (typeof config.stream_stale_sec === 'number' && config.stream_stale_sec > 0) {
                return config.stream_stale_sec;
            }
        } catch (_) {}
    }
    return 30;
}

const STREAM_STALE_SEC = loadStreamStaleSec();

function loadFoamMetricsConfig() {
    const fromEnv = {
        enabled: process.env.FOAM_METRICS_ENABLED === 'true',
        window_minutes: Number(process.env.FOAM_METRICS_WINDOW_MIN || 1),
        refresh_sec: Number(process.env.FOAM_METRICS_REFRESH_SEC || 15),
        database_host: process.env.FOAM_DB_HOST,
        database_port: process.env.FOAM_DB_PORT ? Number(process.env.FOAM_DB_PORT) : undefined,
        database_name: process.env.FOAM_DB_NAME,
        database_username: process.env.FOAM_DB_USER,
        database_password: process.env.FOAM_DB_PASSWORD
    };

    const defaults = {
        enabled: false,
        window_minutes: 1,
        refresh_sec: 15,
        database_host: 'postgres',
        database_port: 5432,
        database_name: 'foam_v2',
        database_username: 'postgres',
        database_password: ''
    };

    let fromFile = {};
    if (CONFIG_FILE && fs.existsSync(CONFIG_FILE)) {
        try {
            const config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
            fromFile = config.foam_metrics || {};
        } catch (_) {}
    }

    const merged = { ...defaults, ...fromFile };
    for (const [key, value] of Object.entries(fromEnv)) {
        if (value !== undefined && value !== '' && value !== false) {
            merged[key] = value;
        }
    }
    if (fromFile.enabled === true && process.env.FOAM_METRICS_ENABLED !== 'false') {
        merged.enabled = true;
    }
    merged.database_port = Number(merged.database_port || 5432);
    return merged;
}

const FOAM_METRICS = loadFoamMetricsConfig();

/** 90.1.1 → FM_90-1-1 (как в foam_v2_1) */
function toFoamCamId(shortId) {
    return `FM_${shortId.replace(/\./g, '-')}`;
}

/** FM_90-1-1 → 90.1.1 */
function fromFoamCamId(foamId) {
    if (!foamId || !foamId.startsWith('FM_')) return foamId;
    return foamId.slice(3).replace(/-/g, '.');
}

let metricsPool = null;
let metricsCache = { data: {}, fetchedAt: 0, error: null };

function getMetricsPool() {
    if (!pgPool || !FOAM_METRICS.enabled) return null;
    if (!metricsPool) {
        metricsPool = new pgPool({
            host: FOAM_METRICS.database_host,
            port: FOAM_METRICS.database_port,
            database: FOAM_METRICS.database_name,
            user: FOAM_METRICS.database_username,
            password: FOAM_METRICS.database_password,
            max: 4,
            idleTimeoutMillis: 30000,
            connectionTimeoutMillis: 5000
        });
        metricsPool.on('error', (err) => {
            console.error('Foam metrics pool error:', err.message);
        });
    }
    return metricsPool;
}

async function fetchFoamMetricsFromDb() {
    const pool = getMetricsPool();
    if (!pool) return {};

    const windowMin = Math.max(1, Number(FOAM_METRICS.window_minutes) || 1);
    const result = await pool.query(
        `SELECT cam, feature, AVG(value)::double precision AS avg_value,
                MAX(log_datetime) AS last_update
         FROM features
         WHERE log_datetime >= NOW() - ($1::text || ' minutes')::interval
           AND feature IN ('obj_count', 'obj_area_mean_cm2', 'obj_area_mean')
         GROUP BY cam, feature`,
        [String(windowMin)]
    );

    const byCamera = {};
    for (const row of result.rows) {
        const shortId = fromFoamCamId(row.cam);
        if (!byCamera[shortId]) {
            byCamera[shortId] = { updatedAt: null };
        }
        if (row.feature === 'obj_count') {
            byCamera[shortId].bubbleCount = Math.round(row.avg_value);
        } else if (row.feature === 'obj_area_mean_cm2') {
            byCamera[shortId].areaCm2 = Number(row.avg_value);
            byCamera[shortId].areaUnit = 'cm2';
        } else if (row.feature === 'obj_area_mean' && byCamera[shortId].areaCm2 == null) {
            byCamera[shortId].areaPx2 = Number(row.avg_value);
            byCamera[shortId].areaUnit = 'px2';
        }
        const ts = row.last_update ? new Date(row.last_update).toISOString() : null;
        if (ts && (!byCamera[shortId].updatedAt || ts > byCamera[shortId].updatedAt)) {
            byCamera[shortId].updatedAt = ts;
        }
    }

    return byCamera;
}

async function getFoamMetrics(force = false) {
    if (!FOAM_METRICS.enabled) return { metrics: {}, enabled: false };

    const ttlMs = Math.max(5000, (Number(FOAM_METRICS.refresh_sec) || 15) * 1000);
    if (!force && metricsCache.fetchedAt && (Date.now() - metricsCache.fetchedAt) < ttlMs) {
        return { metrics: metricsCache.data, enabled: true, error: metricsCache.error };
    }

    try {
        const data = await fetchFoamMetricsFromDb();
        metricsCache = { data, fetchedAt: Date.now(), error: null };
        return { metrics: data, enabled: true, error: null };
    } catch (error) {
        console.error('Foam metrics query failed:', error.message);
        metricsCache.error = error.message;
        return { metrics: metricsCache.data, enabled: true, error: error.message };
    }
}

function isInside(parent, child) {
    const relative = path.relative(parent, child);
    return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

app.use(express.static(PUBLIC_DIR));
app.use(express.json());

function stripCameraPrefix(folderName) {
    if (folderName.startsWith(CAMERA_FOLDER_PREFIX)) {
        return folderName.slice(CAMERA_FOLDER_PREFIX.length);
    }
    return folderName;
}

function getSectionFromCameraId(folderName) {
    const shortId = stripCameraPrefix(folderName);
    const dot = shortId.indexOf('.');
    return dot > 0 ? shortId.slice(0, dot) : shortId;
}

/** Линия = первые два сегмента ID, например 90.2 из 90.2.4 */
function getLineFromCameraId(folderName) {
    const shortId = stripCameraPrefix(folderName);
    const parts = shortId.split('.');
    if (parts.length >= 2) return `${parts[0]}.${parts[1]}`;
    return shortId;
}

function findPlaylistFile(cameraPath) {
    for (const fileName of ['index.m3u8', 'stream.m3u8']) {
        if (fs.existsSync(path.join(cameraPath, fileName))) return fileName;
    }
    return 'index.m3u8';
}

function getLatestSegmentMtime(cameraPath) {
    if (!fs.existsSync(cameraPath)) return 0;

    let latest = 0;
    try {
        for (const name of fs.readdirSync(cameraPath)) {
            if (!name.endsWith('.ts')) continue;
            const stat = fs.statSync(path.join(cameraPath, name));
            if (stat.mtimeMs > latest) latest = stat.mtimeMs;
        }
    } catch (_) {}

    return latest;
}

function isStreamFresh(cameraPath, m3u8Path) {
    const lastSegmentAt = getLatestSegmentMtime(cameraPath);
    if (lastSegmentAt > 0) {
        return {
            lastSegmentAt,
            hasStream: (Date.now() - lastSegmentAt) < STREAM_STALE_SEC * 1000
        };
    }

    if (fs.existsSync(m3u8Path)) {
        try {
            const mtime = fs.statSync(m3u8Path).mtimeMs;
            return {
                lastSegmentAt: mtime,
                hasStream: (Date.now() - mtime) < STREAM_STALE_SEC * 1000
            };
        } catch (_) {}
    }

    return { lastSegmentAt: null, hasStream: false };
}

function loadExpectedCameraIds() {
    if (!CONFIG_FILE || !fs.existsSync(CONFIG_FILE)) return [];

    try {
        const config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
        const mapping = config.camera_mapping || {};
        const ids = new Set();

        for (const cameraId of Object.values(mapping)) {
            if (typeof cameraId !== 'string') continue;
            const trimmed = cameraId.trim();
            if (!trimmed) continue;
            ids.add(`${CAMERA_FOLDER_PREFIX}${trimmed}`);
        }

        return Array.from(ids);
    } catch (error) {
        console.error(`Failed to read ${CONFIG_FILE}:`, error.message);
        return [];
    }
}

function attachMetrics(camera, metricsByName) {
    const m = metricsByName[camera.name];
    if (!m) return camera;
    return { ...camera, metrics: m };
}

function getCameraData(folderName, metricsByName = {}) {
    const cameraPath = path.join(HLS_OUTPUT_DIR, folderName);
    const playlistFile = findPlaylistFile(cameraPath);
    const m3u8Path = path.join(cameraPath, playlistFile);
    const shortId = stripCameraPrefix(folderName);
    const { lastSegmentAt, hasStream } = isStreamFresh(cameraPath, m3u8Path);

    return attachMetrics({
        id: folderName,
        name: shortId,
        section: getSectionFromCameraId(folderName),
        line: getLineFromCameraId(folderName),
        streamUrl: `/hls/${encodeURIComponent(folderName)}/${playlistFile}`,
        hasStream,
        lastSegmentAt
    }, metricsByName);
}

function getCameras(filterIds = null, metricsByName = {}) {
    try {
        const ids = new Set(loadExpectedCameraIds());

        if (fs.existsSync(HLS_OUTPUT_DIR)) {
            const entries = fs.readdirSync(HLS_OUTPUT_DIR, { withFileTypes: true });
            entries
                .filter(entry => entry.isDirectory())
                .forEach(entry => ids.add(entry.name));
        } else {
            console.error(`Directory ${HLS_OUTPUT_DIR} does not exist`);
        }

        let cameras = Array.from(ids).map(id => getCameraData(id, metricsByName));

        if (filterIds && filterIds.length > 0) {
            const cameraMap = new Map(cameras.map(camera => [camera.id, camera]));
            cameras = filterIds
                .filter(id => cameraMap.has(id))
                .map(id => ({ ...cameraMap.get(id) }));
        }

        return cameras.sort((a, b) => {
            const sectionCompare = a.section.localeCompare(b.section, undefined, { numeric: true });
            if (sectionCompare !== 0) return sectionCompare;
            const lineCompare = a.line.localeCompare(b.line, undefined, { numeric: true });
            if (lineCompare !== 0) return lineCompare;
            return a.name.localeCompare(b.name, undefined, { numeric: true });
        });
    } catch (error) {
        console.error('Error reading cameras:', error);
        return [];
    }
}

function getBuiltinSectionConfigs(cameras = getCameras()) {
    const bySection = new Map();

    for (const camera of cameras) {
        if (!bySection.has(camera.section)) {
            bySection.set(camera.section, []);
        }
        bySection.get(camera.section).push(camera.id);
    }

    return Array.from(bySection.entries())
        .sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }))
        .map(([section, cameraIds]) => ({
            id: `section:${section}`,
            name: `Секция ${section}`,
            builtin: true,
            cameras: cameraIds.sort((a, b) => {
                const lineA = getLineFromCameraId(a);
                const lineB = getLineFromCameraId(b);
                const lineCompare = lineA.localeCompare(lineB, undefined, { numeric: true });
                if (lineCompare !== 0) return lineCompare;
                return stripCameraPrefix(a).localeCompare(stripCameraPrefix(b), undefined, { numeric: true });
            })
        }));
}

function ensureDataDir() {
    fs.mkdirSync(DATA_DIR, { recursive: true });
}

function loadSavedConfigs() {
    try {
        if (!fs.existsSync(CONFIGS_FILE)) return [];
        const parsed = JSON.parse(fs.readFileSync(CONFIGS_FILE, 'utf8'));
        return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
        console.error('Error reading configs:', error.message);
        return [];
    }
}

function saveConfigs(configs) {
    ensureDataDir();
    fs.writeFileSync(CONFIGS_FILE, JSON.stringify(configs, null, 2), 'utf8');
}

app.get('/api/cameras', async (req, res) => {
    let filterIds = null;
    if (req.query.ids) {
        filterIds = req.query.ids.split('-')
            .map(id => id.trim())
            .filter(id => id !== '');
    }

    let metricsByName = {};
    if (FOAM_METRICS.enabled) {
        const { metrics } = await getFoamMetrics();
        metricsByName = metrics;
    }

    res.json(getCameras(filterIds, metricsByName));
});

app.get('/api/metrics', async (req, res) => {
    const force = req.query.force === '1' || req.query.force === 'true';
    const result = await getFoamMetrics(force);
    res.json({
        enabled: result.enabled,
        windowMinutes: FOAM_METRICS.window_minutes,
        error: result.error || null,
        metrics: result.metrics
    });
});

app.get('/api/sections', (req, res) => {
    res.json(getBuiltinSectionConfigs());
});

app.get('/api/configs', (req, res) => {
    const cameras = getCameras();
    res.json([...getBuiltinSectionConfigs(cameras), ...loadSavedConfigs()]);
});

app.post('/api/configs', (req, res) => {
    const { name, cameras } = req.body || {};

    if (typeof name !== 'string' || name.trim() === '') {
        return res.status(400).json({ error: 'Введите название конфигурации' });
    }
    if (!Array.isArray(cameras) || cameras.length === 0) {
        return res.status(400).json({ error: 'Выберите хотя бы одну камеру' });
    }

    const configs = loadSavedConfigs();
    if (configs.length >= MAX_CONFIGS) {
        return res.status(409).json({ error: `Достигнут лимит в ${MAX_CONFIGS} конфигураций` });
    }

    const config = {
        id: crypto.randomUUID(),
        name: name.trim().slice(0, 60),
        builtin: false,
        cameras: cameras.filter(c => typeof c === 'string' && c.trim() !== '')
    };

    try {
        saveConfigs([...configs, config]);
    } catch (error) {
        console.error('Error saving configs:', error.message);
        return res.status(500).json({ error: 'Не удалось сохранить конфигурацию' });
    }

    res.status(201).json(config);
});

app.delete('/api/configs/:id', (req, res) => {
    if (req.params.id.startsWith('section:')) {
        return res.status(403).json({ error: 'Встроенную секцию удалить нельзя' });
    }

    const configs = loadSavedConfigs();
    const next = configs.filter(c => c.id !== req.params.id);

    if (next.length === configs.length) {
        return res.status(404).json({ error: 'Конфигурация не найдена' });
    }

    try {
        saveConfigs(next);
    } catch (error) {
        console.error('Error saving configs:', error.message);
        return res.status(500).json({ error: 'Не удалось удалить конфигурацию' });
    }

    res.json({ ok: true });
});

app.get('/hls/:camera/:file', (req, res) => {
    const { camera, file } = req.params;
    const filePath = path.resolve(HLS_OUTPUT_DIR, camera, file);

    if (!isInside(HLS_OUTPUT_DIR, filePath)) {
        return res.status(403).send('Forbidden');
    }

    fs.stat(filePath, (err, stats) => {
        if (err || !stats.isFile()) {
            return res.status(404).send('File not found');
        }

        if (file.endsWith('.m3u8')) {
            res.setHeader('Content-Type', 'application/vnd.apple.mpegurl');
            res.setHeader('Cache-Control', 'no-cache');
        } else if (file.endsWith('.ts')) {
            res.setHeader('Content-Type', 'video/MP2T');
            res.setHeader('Cache-Control', 'no-cache');
        }

        res.sendFile(filePath);
    });
});

app.get('/', (req, res) => {
    res.setHeader('Cache-Control', 'no-cache');
    res.sendFile(path.join(PUBLIC_DIR, 'index.html'));
});

app.get('/style.css', (req, res) => {
    res.setHeader('Cache-Control', 'no-cache');
    res.sendFile(path.join(PUBLIC_DIR, 'style.css'));
});

app.listen(PORT, () => {
    ensureDataDir();
    const cameras = getCameras();
    const sections = getBuiltinSectionConfigs(cameras);

    console.log(`Server running on http://localhost:${PORT}`);
    console.log(`Cameras dir: ${HLS_OUTPUT_DIR}`);
    console.log(`Config file: ${CONFIG_FILE || '(not set)'}`);
    console.log(`Stream stale threshold: ${STREAM_STALE_SEC}s`);
    console.log(`Configs file: ${CONFIGS_FILE}`);
    console.log(`Cameras: ${cameras.length}, sections: ${sections.length}`);
    console.log(`Foam metrics: ${FOAM_METRICS.enabled ? 'enabled' : 'disabled'}` +
        (FOAM_METRICS.enabled ? ` (window ${FOAM_METRICS.window_minutes} min, DB ${FOAM_METRICS.database_host})` : ''));

    if (!chokidar) return;

    const watcher = chokidar.watch(HLS_OUTPUT_DIR, {
        ignored: /(^|[\/\\])\../,
        persistent: true,
        depth: 0,
        usePolling: process.env.CHOKIDAR_USEPOLLING === 'true'
    });

    watcher.on('addDir', (dirPath) => {
        console.log(`New camera directory detected: ${dirPath}`);
    });

    watcher.on('unlinkDir', (dirPath) => {
        console.log(`Camera directory removed: ${dirPath}`);
    });
});

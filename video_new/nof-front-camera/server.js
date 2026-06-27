const express = require('express');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

let chokidar = null;
try {
    chokidar = require('chokidar');
} catch (error) {
    console.warn('chokidar is not installed; directory watch logs are disabled.');
}

const app = express();
const PORT = Number(process.env.PORT || 4000);

const HLS_OUTPUT_DIR = process.env.HLS_OUTPUT_DIR || '/hls_output';
const CONFIG_FILE = process.env.CONFIG_FILE || '';
const CAMERA_FOLDER_PREFIX = process.env.CAMERA_FOLDER_PREFIX || 'camera_';
const PUBLIC_DIR = path.join(__dirname, 'public');

const DATA_DIR = process.env.DATA_DIR || '/app/data';
const CONFIGS_FILE = path.join(DATA_DIR, 'configs.json');
const MAX_CONFIGS = 7;

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

function findPlaylistFile(cameraPath) {
    for (const fileName of ['index.m3u8', 'stream.m3u8']) {
        if (fs.existsSync(path.join(cameraPath, fileName))) return fileName;
    }
    return 'index.m3u8';
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

function getCameraData(folderName) {
    const cameraPath = path.join(HLS_OUTPUT_DIR, folderName);
    const playlistFile = findPlaylistFile(cameraPath);
    const m3u8Path = path.join(cameraPath, playlistFile);
    const shortId = stripCameraPrefix(folderName);

    return {
        id: folderName,
        name: shortId,
        section: getSectionFromCameraId(folderName),
        streamUrl: `/hls/${encodeURIComponent(folderName)}/${playlistFile}`,
        hasStream: fs.existsSync(m3u8Path)
    };
}

function getCameras(filterIds = null) {
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

        let cameras = Array.from(ids).map(getCameraData);

        if (filterIds && filterIds.length > 0) {
            const cameraMap = new Map(cameras.map(camera => [camera.id, camera]));
            cameras = filterIds
                .filter(id => cameraMap.has(id))
                .map(id => ({ ...cameraMap.get(id) }));
        }

        return cameras.sort((a, b) => {
            const sectionCompare = a.section.localeCompare(b.section, undefined, { numeric: true });
            if (sectionCompare !== 0) return sectionCompare;
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
            cameras: cameraIds.sort((a, b) =>
                stripCameraPrefix(a).localeCompare(stripCameraPrefix(b), undefined, { numeric: true })
            )
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

app.get('/api/cameras', (req, res) => {
    let filterIds = null;
    if (req.query.ids) {
        filterIds = req.query.ids.split('-')
            .map(id => id.trim())
            .filter(id => id !== '');
    }
    res.json(getCameras(filterIds));
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
    const filePath = path.join(HLS_OUTPUT_DIR, camera, file);

    const normalizedPath = path.normalize(filePath);
    if (!normalizedPath.startsWith(HLS_OUTPUT_DIR)) {
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
    res.sendFile(path.join(PUBLIC_DIR, 'index.html'));
});

app.listen(PORT, () => {
    ensureDataDir();
    const cameras = getCameras();
    const sections = getBuiltinSectionConfigs(cameras);

    console.log(`Server running on http://localhost:${PORT}`);
    console.log(`Cameras dir: ${HLS_OUTPUT_DIR}`);
    console.log(`Config file: ${CONFIG_FILE || '(not set)'}`);
    console.log(`Configs file: ${CONFIGS_FILE}`);
    console.log(`Cameras: ${cameras.length}, sections: ${sections.length}`);

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

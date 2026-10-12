// The demo backend, assembled: importing this file fills the route table.
//
// To add a page's routes, write routes/<name>.js with route(...) calls and import it
// here. `tests/unit/test_demo_contract.py` lists what is still missing.

import './routes/system.js';
import './routes/instances.js';
import './routes/net.js';
import './routes/control.js';
import './routes/stream.js';
import './routes/media.js';
import './routes/automation.js';
import './routes/backups.js';
import './routes/approvals.js';
import './routes/ai.js';
import { media, socket } from './backend.js';

// Pictures and sockets: listed here, served by transport.js (see backend.js).
media('/api/instances/{id}/frame.jpg', 'frame');
media('/api/instances/{id}/stream.mjpg', 'stream');
media('/api/instances/{id}/screenshot', 'screenshot');
media('/api/instances/{id}/files/pull', 'file');
socket('/api/events', 'events');
socket('/api/instances/{id}/h264', 'h264');

export { handle, listRoutes, served } from './backend.js';
export { advance } from './farm.js';
export { db, reset, save, setClock, subscribe } from './store.js';

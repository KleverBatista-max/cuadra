/* Puente: da a la página la misma interfaz que tenía en Claude (window.claude.use),
   pero contra el servidor de la Raspberry. Los cambios se ven al momento y cada 10 s. */
(function () {
  var listeners = [];
  var version = null;

  function api(method, url, body) {
    var opt = { method: method, headers: {}, credentials: 'same-origin' };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      if (r.status === 401) { location.href = '/login'; throw new Error('Sesión vencida'); }
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || ('Error ' + r.status)); });
      var ct = r.headers.get('content-type') || '';
      return ct.indexOf('json') >= 0 ? r.json() : null;
    });
  }

  function snap(rows) {
    return {
      metadata: { fromCache: false },
      docs: rows.map(function (x) { return { id: x.id, data: function () { return x.data; } }; })
    };
  }

  function Query(coll, filtro) { this.coll = coll; this.filtro = filtro || null; }
  Query.prototype.where = function (f, op, v) { return new Query(this.coll, { f: f, op: op, v: v }); };
  Query.prototype.url = function () {
    var u = '/api/c/' + encodeURIComponent(this.coll);
    if (this.filtro) u += '?f=' + encodeURIComponent(this.filtro.f) + '&op=' + encodeURIComponent(this.filtro.op) + '&v=' + encodeURIComponent(this.filtro.v);
    return u;
  };
  Query.prototype.get = function () { return api('GET', this.url()).then(snap); };
  Query.prototype.onSnapshot = function (ok, err) {
    var q = this, l = { q: q, ok: ok, err: err, vivo: true };
    listeners.push(l);
    cargar(l);
    return function () { l.vivo = false; listeners = listeners.filter(function (x) { return x !== l; }); };
  };
  Query.prototype.doc = function (id) { return new Doc(this.coll, id); };

  function Doc(coll, id) { this.coll = coll; this.id = id; }
  Doc.prototype.url = function () { return '/api/d/' + encodeURIComponent(this.coll) + '/' + encodeURIComponent(this.id); };
  Doc.prototype.set = function (data) { return api('PUT', this.url(), data).then(refrescar); };
  Doc.prototype.update = function (data) { return api('PATCH', this.url(), data).then(refrescar); };
  Doc.prototype.delete = function () { return api('DELETE', this.url()).then(refrescar); };

  function cargar(l) {
    api('GET', l.q.url()).then(function (rows) { if (l.vivo) l.ok(snap(rows)); }, function (e) { if (l.vivo && l.err) l.err(e); });
  }
  function refrescar() { listeners.slice().forEach(cargar); }

  setInterval(function () {
    if (document.hidden) return;
    api('GET', '/api/version').then(function (r) {
      if (version !== null && r.v !== version) refrescar();
      version = r.v;
    }, function () {});
  }, 10000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refrescar(); });

  var db = {
    collection: function (c) { return new Query(c); },
    doc: function (path) { var p = path.split('/'); return new Doc(p[0], p.slice(1).join('/')); }
  };

  var assets = {
    upload: function (file) {
      var fd = new FormData(); fd.append('archivo', file, file.name || 'factura');
      return fetch('/api/subir', { method: 'POST', body: fd, credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(t || 'No se pudo subir'); });
        return r.json();
      });
    }
  };

  var downloads = {
    save: function (o) {
      var blob = new Blob([o.data], { type: 'application/json' });
      var a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = o.filename;
      document.body.appendChild(a); a.click(); a.remove();
      return Promise.resolve();
    }
  };

  var U = window.__USUARIO__ || {};
  var user = { can: function () { return Promise.resolve(true); }, nombre: U.nombre, rol: U.rol };

  window.claude = {
    use: function (what) {
      if (what === 'db') return Promise.resolve(db);
      if (what === 'assets') return Promise.resolve(assets);
      if (what === 'downloads') return Promise.resolve(downloads);
      if (what === 'user') return Promise.resolve(user);
      return Promise.resolve(null); // sin IA: la factura la lee el servidor
    }
  };
  window.__api = api;
  window.__refrescar = refrescar;
})();

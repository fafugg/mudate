// ── Map & geocoding state and methods ─────────────────────────────────────────
// Spread into the Alpine component returned by app() in app.js.

const mapMethods = {
  mapView: false,
  _map: null,
  _markerLayer: null,
  _mapUserMoved: false,
  _mapMoving: false,
  _pendingRedraw: false,
  _fittedKey: '',
  _savedCenter: null,
  _savedZoom: null,
  _geocodeAttemptedSession: null,
  _geocodeAttemptedCount: -1,
  _redrawTimer: null,
  geocodeStatus: null,
  _geocodePoll: null,
  _geocodeRunId: null,
  _geocodingDetailHouseId: null,
  _mapFocusHouse: null,
  _focusActive: false,
  _markerMap: {},
  mapMissingCount: 0,
  mapLegend: MAP_LEGEND,

  initMap() {
    this._resizeMap();
    // The view may have been switched while a geocode fetch was in flight,
    // which unmounts #map via x-if. Abort instead of throwing "Map container not found".
    if (!document.getElementById('map')) return;
    if (this._map) {
      this._map.invalidateSize({ animate: false });
      this.renderPins();
      return;
    }
    // preferCanvas renders circle markers on <canvas> instead of SVG paths:
    // drastically cheaper to redraw during pan/zoom with hundreds of pins.
    const start = (this._savedCenter && this._savedZoom)
      ? { center: this._savedCenter, zoom: this._savedZoom }
      : { center: [-34.61, -58.44], zoom: 12 };
    this._map = L.map('map', { preferCanvas: true }).setView(start.center, start.zoom);
    if (this._savedCenter && this._savedZoom) {
      // Restoring a previously positioned map: don't let auto-fit override it.
      this._mapUserMoved = true;
    }
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
      fadeAnimation: false,     // tiles pop in instantly, no fade compositing
      updateWhenZooming: false,  // don't fetch tiles mid-zoom, only when it settles
      keepBuffer: 4,             // keep a wider tile margin so panning has no gaps
    }).addTo(this._map);
    // Track map movement so renderPins() can defer heavy work during animations.
    this._map.on('movestart zoomstart', () => { this._mapMoving = true; });
    this._map.on('moveend zoomend', () => {
      this._mapMoving = false;
      this._mapUserMoved = true;
      if (this._pendingRedraw) {
        this._pendingRedraw = false;
        this.renderPins();
      }
    });
    requestAnimationFrame(() => {
      this._map?.invalidateSize({ animate: false });
      this.renderPins();
    });
  },

  renderPins() {
    if (!this._map) return;
    // While the map is panning/zooming, skip the heavy redraw and do a single
    // deferred pass once it settles (avoids fighting for animation frames).
    if (this._mapMoving) {
      this._pendingRedraw = true;
      return;
    }
    if (!this._markerLayer) {
      this._markerLayer = L.layerGroup().addTo(this._map);
    }

    const all = this.filteredHouses;
    const placed = all.filter(h => h.lat != null && h.lng != null);
    this.mapMissingCount = all.filter(h => h.lat == null && (h.manual_address || h.address)).length;

    // Incremental update: remove markers for houses no longer in filtered set
    const placedIds = new Set(placed.map(h => h.internal_id));
    for (const id of Object.keys(this._markerMap)) {
      if (!placedIds.has(id)) {
        const m = this._markerMap[id];
        this._markerLayer.removeLayer(m);
        delete this._markerMap[id];
      }
    }

    // Add/update markers incrementally
    placed.forEach(h => {
      const id = h.internal_id;
      const existing = this._markerMap[id];
      if (existing) {
        // Update existing marker
        existing.setLatLng([h.lat, h.lng]);
        existing.setStyle({ fillColor: this.pinColor(h) });
        existing.setTooltipContent(
          `<b>${this.escHtml(h.manual_address || h.address || '')}</b><br>${this.escHtml(this.formatPrice(h.price, h.currency))}`
        );
      } else {
        // Create new marker
        const marker = L.circleMarker([h.lat, h.lng], {
          radius: 9,
          fillColor: this.pinColor(h),
          color: '#ffffff',
          weight: 2,
          opacity: 1,
          fillOpacity: 0.85,
        });
        marker.bindTooltip(
          `<b>${this.escHtml(h.manual_address || h.address || '')}</b><br>${this.escHtml(this.formatPrice(h.price, h.currency))}`
        );
        marker.on('click', () => this.openDetail(h));
        this._markerLayer.addLayer(marker);
        this._markerMap[id] = marker;
      }
    });

    // Auto-fit bounds when opening the map without a focus target.
    // Memoized: only recompute bounds when the marker set actually changes.
    if (placed.length > 0 && !this._mapUserMoved && !this._focusActive) {
      const key = placed.map(h => `${h.internal_id}:${h.lat}:${h.lng}`).join('|');
      if (key !== this._fittedKey) {
        this._fittedKey = key;
        this.fitBounds();
      }
    }

    // Focus on a specific house (from viewInMap)
    if (this._mapFocusHouse && !this._focusActive) {
      const fh = this._mapFocusHouse;
      this._focusActive = true;
      this._mapFocusHouse = null;
      if (fh.lat != null) {
        this._map.flyTo([fh.lat, fh.lng], 15, { animate: true, duration: 0.8 });
        const m = this._markerMap[fh.internal_id];
        if (m) {
          m.setRadius(13);
          m.setStyle({ weight: 3, color: '#1d4ed8', fillOpacity: 1 });
          m.bringToFront();
          m.openTooltip();
          this._map.once('moveend', () => {
            this._focusActive = false;
            const tm = this._markerMap[fh.internal_id];
            if (tm) {
              tm.setRadius(9);
              tm.setStyle({ weight: 2, color: '#ffffff', fillOpacity: 0.85 });
            }
          });
        } else {
          this._focusActive = false;
        }
      } else {
        this._focusActive = false;
      }
    }
  },

  fitBounds() {
    if (!this._map || !this._markerLayer) return;
    this._mapUserMoved = false;
    const layers = this._markerLayer.getLayers();
    if (layers.length > 0) {
      this._map.fitBounds(L.featureGroup(layers).getBounds().pad(0.35), { maxZoom: 14 });
    }
  },

  updateMarkerColor(h) {
    const m = this._markerMap[h.internal_id];
    if (m) {
      m.setStyle({ fillColor: this.pinColor(h) });
    }
  },

  pinColor(h) {
    return PIN_COLORS[h.status] || PIN_COLORS[h.review] || PIN_COLORS.default;
  },

  _resizeMap() {
    const el = document.getElementById('map-container');
    if (!el) return;
    const top = el.getBoundingClientRect().top;
    const vh = (window.visualViewport?.height ?? window.innerHeight);
    el.style.height = Math.max(200, vh - top - 16) + 'px';
  },

  // Fully tear down the Leaflet map so nothing keeps running (tile layer,
  // canvas renderer, event listeners) while the map is closed in the background.
  destroyMap() {
    if (this._map && this._map._loaded) {
      this._savedCenter = this._map.getCenter();
      this._savedZoom = this._map.getZoom();
    }
    if (this._map) {
      this._map.remove();
      this._map = null;
    }
    this._markerLayer = null;
    this._markerMap = {};
    this._fittedKey = '';
    this._mapMoving = false;
    this._pendingRedraw = false;
    this._mapUserMoved = false;
    this._focusActive = false;
  },

  stopGeocoding() {
    if (this._geocodePoll) { clearInterval(this._geocodePoll); this._geocodePoll = null; }
    if (this._geocodeRunId) {
      fetch(`/api/runs/${this._geocodeRunId}`, { method: 'DELETE' }).catch(() => {});
      this._geocodeRunId = null;
    }
    this.geocodeStatus = null;
  },

  async openMap() {
    if (this._geocodeRunId) {
      this.initMap();
      return;
    }
    // Only auto-start geocoding when the set of ungeocoded houses actually
    // changed since the last attempt for this session. Skips the redundant
    // "already_done" POST every time the map is reopened with the same data.
    const needsGeocode = this.houses.filter(h => h.lat == null && (h.manual_address || h.address));
    const sid = this.currentSession?.id;
    const attempted = sid === this._geocodeAttemptedSession
      && needsGeocode.length === this._geocodeAttemptedCount;
    if (needsGeocode.length > 0 && !attempted) {
      try {
        const res = await fetch(
          `/api/users/${this.username}/sessions/${sid}/geocode`,
          { method: 'POST' }
        );
        const data = await res.json();
        if (!data.already_done && data.run_id) {
          this._geocodeRunId = data.run_id;
          this.geocodeStatus = { status: 'running', message: 'Geocodificando…', progress: 0, total: 0 };
          this._geocodePoll = setInterval(() => this.pollGeocode(data.run_id), 2000);
        } else if (data.already_done) {
          // A run already covered this exact set of houses — don't retry on reopen.
          this._geocodeAttemptedSession = sid;
          this._geocodeAttemptedCount = needsGeocode.length;
        }
      } catch (e) { /* show map anyway; retry is allowed on next open */ }
    }
    this.initMap();
  },

  async skipGeocode() {
    this.stopGeocoding();
    this.initMap();
  },

  async pollGeocode(runId) {
    try {
      const status = await api('GET', `/runs/${runId}`);
      const prevProgress = this.geocodeStatus?.progress || 0;
      this.geocodeStatus = status;
      if (status.status === 'running') {
        if (status.progress > prevProgress) {
          const d = await api('GET', `/users/${this.username}/sessions/${this.currentSession.id}`);
          this.houses = d.houses || [];
        }
        return;
      }
      clearInterval(this._geocodePoll);
      this._geocodePoll = null;
      this._geocodeRunId = null;
      if (status.status === 'done') {
        const d = await api('GET', `/users/${this.username}/sessions/${this.currentSession.id}`);
        this.houses = d.houses || [];
        this.geocodeStatus = null;
        // Remember how many houses are still ungeocoded so reopen doesn't re-POST.
        this._geocodeAttemptedSession = this.currentSession?.id;
        this._geocodeAttemptedCount = this.houses.filter(h => h.lat == null && (h.manual_address || h.address)).length;
      }
      if (this.mapView) this.initMap();
    } catch (e) {
      clearInterval(this._geocodePoll);
      this._geocodePoll = null;
      this._geocodeRunId = null;
      if (this.mapView) this.initMap();
    }
  },
};

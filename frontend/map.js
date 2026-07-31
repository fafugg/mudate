// ── Map & geocoding state and methods ─────────────────────────────────────────
// Spread into the Alpine component returned by app() in app.js.

const mapMethods = {
  mapView: false,
  _map: null,
  _markerLayer: null,
  _mapUserMoved: false,
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
    if (this._map) {
      this._map.invalidateSize({ animate: false });
      this.renderPins();
      return;
    }
    this._map = L.map('map').setView([-34.61, -58.44], 12);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(this._map);
    this._map.on('moveend zoomend', () => { this._mapUserMoved = true; });
    requestAnimationFrame(() => {
      this._map?.invalidateSize({ animate: false });
      this.renderPins();
    });
  },

  renderPins() {
    if (!this._map) return;
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
          `<b>${this.escHtml(h.manual_address || h.address || '')}</b><br>${this.escHtml(this.formatPrice(h.price, h.currency))}`,
          { sticky: true }
        );
        marker.on('click', () => this.openDetail(h));
        this._markerLayer.addLayer(marker);
        this._markerMap[id] = marker;
      }
    });

    // Auto-fit bounds when opening the map without a focus target
    if (placed.length > 0 && !this._mapUserMoved && !this._focusActive) this.fitBounds();

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
    const needsGeocode = this.houses.some(h => h.lat == null && (h.manual_address || h.address));
    if (needsGeocode) {
      try {
        const res = await fetch(
          `/api/users/${this.username}/sessions/${this.currentSession.id}/geocode`,
          { method: 'POST' }
        );
        const data = await res.json();
        if (!data.already_done && data.run_id) {
          this._geocodeRunId = data.run_id;
          this.geocodeStatus = { status: 'running', message: 'Geocodificando…', progress: 0, total: 0 };
          this._geocodePoll = setInterval(() => this.pollGeocode(data.run_id), 2000);
        }
      } catch (e) { /* show map anyway */ }
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

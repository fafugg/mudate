// ── Map & geocoding state and methods ─────────────────────────────────────────
// Spread into the Alpine component returned by app() in app.js.

// Anything stored on the Alpine component is wrapped in a Vue reactive proxy.
// Leaflet compares event-listener contexts by identity (Evented._off does
// `l.ctx !== context`), so a raw-vs-proxy mismatch silently fails to unbind an
// event: a tooltip that was open when the detail panel opened kept its
// `zoomanim` listener alive with `_map === null`, and the next zoom threw
// "Cannot read properties of null (reading '_latLngToNewLayerPoint')".
// `__v_skip` is Vue's markRaw flag (Alpine bundles @vue/reactivity but does not
// expose markRaw), so Leaflet handles stay raw however they are read back.
const markRaw = (obj) => {
  if (obj && typeof obj === 'object') obj.__v_skip = true;
  return obj;
};

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
  _mapRO: null,
  geocodeStatus: null,
  _geocodePoll: null,
  _geocodeRunId: null,
  _geocodingDetailHouseId: null,
  _mapFocusHouse: null,
  _focusActive: false,
  _markerMap: {},
  // Mini map embedded in the fullscreen house-detail modal.
  _detailMap: null,
  _detailMapMarker: null,
  _detailRO: null,
  // Ctrl+drag rubber-band selection (desktop only).
  _boxEl: null,
  _boxStart: null,
  _boxCurrent: null,
  _boxDragging: false,
  _boxMoved: false,
  _boxSwallowClick: false,
  _boxHandlers: null,
  mapMissingCount: 0,
  mapLegend: MAP_LEGEND,
  _leafletPromise: null,

  /** Load Leaflet on first use (CDN, SRI-pinned) instead of in <head>. */
  _loadLeaflet() {
    if (window.L) return Promise.resolve();
    if (this._leafletPromise) return this._leafletPromise;
    this._leafletPromise = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
      css.integrity = 'sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H';
      css.crossOrigin = 'anonymous';
      document.head.appendChild(css);

      const js = document.createElement('script');
      js.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
      js.integrity = 'sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH';
      js.crossOrigin = 'anonymous';
      js.onload = () => resolve();
      js.onerror = () => { this._leafletPromise = null; reject(new Error('leaflet-load-failed')); };
      document.head.appendChild(js);
    });
    return this._leafletPromise;
  },

  async initMap() {
    try {
      await this._loadLeaflet();
    } catch (e) {
      console.error(e);
      this.showToast('No se pudo cargar el mapa (¿sin conexión?).', 'error');
      return;
    }
    // Re-check after the await: the view may have been switched while Leaflet
    // was still downloading, which unmounts #map via x-if.
    this._resizeMap();
    // The view may have been switched while a geocode fetch was in flight,
    // which unmounts #map via x-if. Abort instead of throwing "Map container not found".
    if (!document.getElementById('map')) return;
    this._watchBulkBar();
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
    // boxZoom (Leaflet's Shift+drag "zoom to box") is disabled: the same
    // gesture is repurposed as a Ctrl+drag rubber-band selection below.
    this._map = markRaw(L.map('map', { preferCanvas: true, boxZoom: false }).setView(start.center, start.zoom));
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
    this._initBoxSelect();
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
      this._markerLayer = markRaw(L.layerGroup().addTo(this._map));
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
        existing.setStyle(this.markerStyle(h));
        existing.setTooltipContent(
          `<b>${this.escHtml(h.manual_address || h.address || '')}</b><br>${this.escHtml(this.formatPrice(h.price, h.currency))}`
        );
      } else {
        // Create new marker
        const marker = markRaw(L.circleMarker([h.lat, h.lng], this.markerStyle(h)));
        marker.bindTooltip(
          `<b>${this.escHtml(h.manual_address || h.address || '')}</b><br>${this.escHtml(this.formatPrice(h.price, h.currency))}`
        );
        markRaw(marker._tooltip);
        // Desktop multi-select: Ctrl/Cmd+click toggles the pin in/out of the
        // shared selection instead of opening the detail panel. Real touch
        // events are excluded (some touch devices emulate ctrl on long-press);
        // no pointer-media query is used — it misclassifies touchscreen
        // laptops and mice on some Linux setups, silently disabling this.
        marker.on('click', (e) => {
          const oe = e.originalEvent;
          if (oe.pointerType !== 'touch' && (oe.ctrlKey || oe.metaKey)) {
            this.toggleMapPin(h.internal_id);
            return;
          }
          this.openDetail(h);
        });
        // macOS Ctrl+click also fires contextmenu: suppress it while the
        // modifier is held so the right-click menu doesn't pop up mid-selection.
        marker.on('contextmenu', (e) => {
          if (e.originalEvent?.ctrlKey) e.originalEvent.preventDefault();
        });
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
              // Restore to the marker's own style (keeps the selection ring if
              // this pin is part of a multi-selection).
              const cur = this.houses.find(x => x.internal_id === fh.internal_id) || fh;
              tm.setStyle(this.markerStyle(cur));
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
      m.setStyle(this.markerStyle(h));
    }
  },

  // ── Pin multi-select (desktop only: Ctrl/Cmd+click) ────────────────────────
  // Selection state lives in the shared selectedHouses array (same one the
  // table's checkboxes use), so batchSetReview()/openCompare() work on pins
  // exactly as they do on table rows.

  // Marker style for a house: selected pins keep their review color but gain a
  // larger radius + blue ring so multi-selection is visible on the map.
  markerStyle(h) {
    const sel = this.selectedHouses.includes(h.internal_id);
    return {
      radius: sel ? 13 : 9,
      fillColor: this.pinColor(h),
      color: sel ? '#1d4ed8' : '#ffffff',
      weight: sel ? 3 : 2,
      opacity: 1,
      fillOpacity: sel ? 1 : 0.85,
    };
  },

  refreshMarkerStyle(id) {
    const m = this._markerMap?.[id];
    if (!m) return;
    const h = this.filteredHouses.find(x => x.internal_id === id)
      || this.houses.find(x => x.internal_id === id);
    if (h) m.setStyle(this.markerStyle(h));
  },

  refreshMarkerStyles() {
    if (!this._markerMap) return;
    for (const h of this.filteredHouses) {
      const m = this._markerMap[h.internal_id];
      if (m && h.lat != null) m.setStyle(this.markerStyle(h));
    }
  },

  toggleMapPin(id) {
    this.toggleSelectHouse(id);
    this.refreshMarkerStyle(id);
  },

  // ── Ctrl+drag rubber-band selection (desktop) ──────────────────────────────
  // Drags a marquee over the map and adds every pin whose centre falls inside
  // it — the Windows-Explorer drag-box behaviour. Leaflet's own Shift+drag
  // boxZoom is disabled at map creation so the gesture is unambiguous.
  //
  // Like Ctrl+click this requires a real modifier, so it can never fire from
  // touch or from a plain drag (which must keep panning the map).
  _initBoxSelect() {
    if (this._boxHandlers || !this._map) return;
    const container = this._map.getContainer();

    // The marquee is a plain absolutely-positioned div inside the map
    // container. pointer-events:none so it never swallows the mouseup that
    // ends the drag.
    const el = document.createElement('div');
    el.className = 'leaflet-zoom-box';
    el.style.cssText = 'position:absolute;display:none;z-index:800;pointer-events:none;'
      + 'border:2px dashed #1d4ed8;background:rgba(29,78,216,0.10);border-radius:2px;';
    container.appendChild(el);
    this._boxEl = el;

    const modifiersHeld = (e) => e.ctrlKey || e.metaKey;
    const isTouch = (e) => e.pointerType === 'touch';

    const onDown = (e) => {
      // Any mousedown invalidates a pending click-swallow (see onClick).
      this._boxSwallowClick = false;
      // Left button + Ctrl/Cmd only; touch and plain drags fall through to
      // Leaflet's panning handler untouched.
      if (e.button !== 0 || !modifiersHeld(e) || isTouch(e)) return;
      if (!container.contains(e.target)) return;
      // Never hijack the zoom control / attribution links: Ctrl+click on those
      // should still work as a normal click.
      if (e.target.closest?.('.leaflet-control')) return;
      if (!this._map) return;
      e.preventDefault();
      // stopImmediatePropagation (not stopPropagation): Leaflet's own pan
      // handler is a *sibling* listener on this same node, so plain
      // stopPropagation would still let it start a pan and bind its document
      // mousemove — which then nudged the map a few metres mid-marquee.
      e.stopImmediatePropagation();
      // Ctrl+drag also pans by default; stop it so the map doesn't move while
      // the marquee is being drawn.
      this._map?.dragging?.disable();
      const pt = this._map.mouseEventToContainerPoint(e);
      this._boxStart = pt;
      this._boxCurrent = pt;
      this._boxDragging = true;
      this._boxMoved = false;
      if (this._boxEl) this._boxEl.style.display = 'block';
      this._updateBoxEl();
    };

    const onMove = (e) => {
      if (!this._boxDragging) return;
      if (!this._map) return;   // map torn down mid-drag
      e.preventDefault();
      e.stopImmediatePropagation();
      this._boxCurrent = this._map.mouseEventToContainerPoint(e);
      if (this._boxStart
        && (Math.abs(this._boxCurrent.x - this._boxStart.x) >= 5
          || Math.abs(this._boxCurrent.y - this._boxStart.y) >= 5)) {
        this._boxMoved = true;
      }
      this._updateBoxEl();
    };

    const onUp = (e) => {
      if (!this._boxDragging) return;
      e.stopImmediatePropagation();
      this._boxDragging = false;
      if (this._boxEl) this._boxEl.style.display = 'none';
      // Re-enable after the current event loop turn so Leaflet's own mouseup
      // handling has already run and can't apply a residual pan.
      setTimeout(() => this._map?.dragging?.enable(), 0);
      if (this._boxStart && this._boxCurrent) {
        this._applyBoxSelection(this._boxStart, this._boxCurrent);
      }
      // A drag still ends with a DOM click on whatever sits under the pointer;
      // without this the pin under the release point would open its detail
      // panel (or toggle). Swallow exactly that one click. A no-movement
      // gesture stays a normal click, so Ctrl+click keeps toggling pins.
      if (this._boxMoved) this._boxSwallowClick = true;
      this._boxStart = null;
      this._boxCurrent = null;
    };

    // Runs before Leaflet's container handlers, so the trailing click never
    // reaches the map's hit test.
    const onClick = (e) => {
      if (!this._boxSwallowClick) return;
      this._boxSwallowClick = false;
      e.preventDefault();
      e.stopImmediatePropagation();
    };

    // A pan/zoom starting mid-drag (wheel, keyboard) invalidates the marquee.
    const onCancel = () => { if (this._boxDragging) this._cancelBoxSelect(); };

    // Leaflet binds its pan handler to mousedown on the map container itself
    // (Leaflet 1.9: `new Draggable(map._mapPane, map._container)`), so a
    // capture listener on an ancestor is not enough — the event still reaches
    // that listener. Intercepting at the document in the capture phase runs
    // before anything Leaflet owns and reliably swallows the pan, leaving the
    // marquee drag to this handler alone.
    const host = document;
    host.addEventListener('mousedown', onDown, true);
    host.addEventListener('mousemove', onMove, true);
    host.addEventListener('click', onClick, true);
    // Listen on window so the drag still ends correctly when the pointer is
    // released outside the map.
    window.addEventListener('mouseup', onUp, true);
    // Any touch input cancels a half-drawn marquee (hybrid devices).
    container.addEventListener('touchstart', onCancel, { passive: true });
    this._map.on('movestart zoomstart', onCancel);

    this._boxHandlers = { host, container, onDown, onMove, onUp, onCancel, onClick };
  },

  _updateBoxEl() {
    if (!this._boxEl || !this._boxStart || !this._boxCurrent) return;
    const a = this._boxStart;
    const b = this._boxCurrent;
    const left = Math.min(a.x, b.x);
    const top = Math.min(a.y, b.y);
    this._boxEl.style.left = left + 'px';
    this._boxEl.style.top = top + 'px';
    this._boxEl.style.width = Math.abs(a.x - b.x) + 'px';
    this._boxEl.style.height = Math.abs(a.y - b.y) + 'px';
  },

  _cancelBoxSelect() {
    // Esc during a real drag: the pointer release still produces a click on
    // whatever is underneath, so swallow it (otherwise the aborted drag opens
    // the detail panel of a pin).
    if (this._boxMoved) this._boxSwallowClick = true;
    this._boxDragging = false;
    this._boxStart = null;
    this._boxCurrent = null;
    if (this._boxEl) this._boxEl.style.display = 'none';
    this._map?.dragging?.enable();
  },

  // Adds every pin whose centre lies inside the marquee. Additive, matching
  // Ctrl+click (so a stray box never wipes an existing selection).
  _applyBoxSelection(a, b) {
    if (!this._map) return;
    // Ignore accidental micro-drags (a Ctrl+click that jittered by a pixel).
    if (Math.abs(a.x - b.x) < 5 && Math.abs(a.y - b.y) < 5) return;
    const minX = Math.min(a.x, b.x);
    const maxX = Math.max(a.x, b.x);
    const minY = Math.min(a.y, b.y);
    const maxY = Math.max(a.y, b.y);

    let added = 0;
    for (const h of this.filteredHouses) {
      if (h.lat == null || h.lng == null) continue;
      const p = this._map.latLngToContainerPoint([h.lat, h.lng]);
      if (p.x < minX || p.x > maxX || p.y < minY || p.y > maxY) continue;
      if (this.selectedHouses.includes(h.internal_id)) continue;
      this.selectedHouses.push(h.internal_id);
      added++;
    }
    this.refreshMarkerStyles();
    if (added > 0) {
      this.showToast(`${this.selectedHouses.length} propiedades seleccionadas`, 'success');
    } else {
      this.showToast('No hay propiedades nuevas en el recuadro.', 'info');
    }
  },

  // "Todo lo visible": adds every pin inside the current viewport to the
  // selection (never removes; use Ctrl+click or Limpiar for that).
  selectVisibleInViewport() {
    if (!this._map) return;
    const bounds = this._map.getBounds();
    let added = 0;
    for (const h of this.filteredHouses) {
      if (h.lat == null || h.lng == null) continue;
      if (!bounds.contains([h.lat, h.lng])) continue;
      if (this.selectedHouses.includes(h.internal_id)) continue;
      this.selectedHouses.push(h.internal_id);
      added++;
    }
    this.refreshMarkerStyles();
    if (added > 0) {
      this.showToast(`${this.selectedHouses.length} propiedades seleccionadas`, 'success');
    } else {
      this.showToast('No hay propiedades nuevas en el encuadre.', 'info');
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

  // Recompute the map height whenever the bulk-action bar above it actually
  // gains or loses space. Alpine's x-transition keeps the bar in layout for
  // the whole leave animation and only sets display:none at the end, so a
  // resize triggered at click time (watcher + $nextTick) still measures the
  // bar's space and leaves a ~44px gap at the bottom. A ResizeObserver on the
  // bar fires only when its box really changes — i.e. exactly when the layout
  // frees or takes the space, before the next paint — so the map is resized
  // at the moment the bar disappears, whichever code path cleared the selection.
  _watchBulkBar() {
    if (this._mapRO || typeof ResizeObserver === 'undefined') return;
    const bar = document.getElementById('bulk-bar');
    if (!bar) return;
    this._mapRO = new ResizeObserver(() => {
      if (!this.mapView || !this._map) return;
      this._resizeMap();
      this._map.invalidateSize({ animate: false });
    });
    this._mapRO.observe(bar);
  },

  // Fully tear down the Leaflet map so nothing keeps running (tile layer,
  // canvas renderer, event listeners) while the map is closed in the background.
  destroyMap() {
    if (this._map && this._map._loaded) {
      this._savedCenter = markRaw(this._map.getCenter());
      this._savedZoom = this._map.getZoom();
    }
    if (this._map) {
      this._map.remove();
      this._map = null;
    }
    this._markerLayer = null;
    this._markerMap = {};
    if (this._mapRO) { this._mapRO.disconnect(); this._mapRO = null; }
    if (this._boxHandlers) {
      const { host, onDown, onMove, onUp, onCancel, onClick } = this._boxHandlers;
      host.removeEventListener('mousedown', onDown, true);
      host.removeEventListener('mousemove', onMove, true);
      host.removeEventListener('click', onClick, true);
      window.removeEventListener('mouseup', onUp, true);
      this._boxHandlers.container?.removeEventListener('touchstart', onCancel);
      this._boxHandlers = null;
    }
    this._boxEl?.remove();
    this._boxEl = null;
    this._boxDragging = false;
    this._boxMoved = false;
    this._boxSwallowClick = false;
    this._boxStart = null;
    this._boxCurrent = null;
    this._fittedKey = '';
    this._mapMoving = false;
    this._pendingRedraw = false;
    this._mapUserMoved = false;
    this._focusActive = false;
  },

  // ── Detail-modal mini map ──────────────────────────────────────────────────
  // Small Leaflet map embedded in the fullscreen house-detail modal, zoomed to
  // the house address (like the "Ubicación" card on Zonaprop/Argenprop). Same
  // markRaw rule as the main map (design decision 8): anything Leaflet hands
  // back must stay outside Alpine's reactive proxies.
  //
  // The map is created lazily on first fullscreen open with a geocoded house
  // and reused afterwards (same instance across prev/next navigation); only
  // destroyDetailMap() tears it down.

  async initDetailMap(h) {
    if (!h || h.lat == null || h.lng == null) return;   // placeholder shows instead
    try {
      await this._loadLeaflet();
    } catch (e) {
      console.error(e);
      return;
    }
    // The modal may have been closed (or layout toggled back to compact) while
    // Leaflet was still downloading.
    const el = document.getElementById('detail-map');
    if (!el || !this.detailHouse) return;
    if (this._detailMap) {
      this._detailMap.invalidateSize({ animate: false });
      this.updateDetailMap(h);
      return;
    }
    this._detailMap = markRaw(L.map('detail-map', {
      preferCanvas: true,
      attributionControl: true,
      scrollWheelZoom: false,   // page scroll must not get hijacked inside the modal
    }).setView([h.lat, h.lng], 16));
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
      fadeAnimation: false,
      updateWhenZooming: false,
      keepBuffer: 4,
    }).addTo(this._detailMap);
    this.updateDetailMap(h);
    // The container can resize without a window event (layout toggle, font
    // load, images loading above it): keep tiles crisp via ResizeObserver.
    if (typeof ResizeObserver !== 'undefined') {
      this._detailRO = new ResizeObserver(() => {
        this._detailMap?.invalidateSize({ animate: false });
      });
      this._detailRO.observe(el);
    }
  },

  // Re-center and restyle the mini map for the given house (or hide the marker
  // when it has no coordinates yet).
  updateDetailMap(h) {
    if (!this._detailMap || !h) return;
    if (h.lat == null || h.lng == null) {
      if (this._detailMapMarker) {
        this._detailMap.removeLayer(this._detailMapMarker);
        this._detailMapMarker = null;
      }
      return;
    }
    this._detailMap.setView([h.lat, h.lng], 16, { animate: false });
    if (this._detailMapMarker) {
      this._detailMapMarker.setLatLng([h.lat, h.lng]);
      this._detailMapMarker.setStyle(this.markerStyle(h));
    } else {
      this._detailMapMarker = markRaw(L.circleMarker([h.lat, h.lng], this.markerStyle(h)).addTo(this._detailMap));
    }
  },

  // Keep the mini-map pin in sync when the review changes from the modal footer.
  refreshDetailMarkerStyle() {
    const h = this.detailHouse;
    if (this._detailMapMarker && h) this._detailMapMarker.setStyle(this.markerStyle(h));
  },

  destroyDetailMap() {
    if (this._detailRO) { this._detailRO.disconnect(); this._detailRO = null; }
    if (this._detailMap) {
      this._detailMap.remove();
      this._detailMap = null;
    }
    this._detailMapMarker = null;
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
      await this.initMap();
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
    await this.initMap();
  },

  async skipGeocode() {
    this.stopGeocoding();
    await this.initMap();
  },

  async pollGeocode(runId) {
    try {
      const status = await api('GET', `/runs/${runId}`);
      const prevProgress = this.geocodeStatus?.progress || 0;
      this.geocodeStatus = status;
      if (status.status === 'running') {
        if (status.progress > prevProgress) {
          // Merge only the houses whose coordinates changed in this run. The
          // old flow refetched the whole 1.7–2.6 MB session payload on every
          // 2000 ms tick (~350 MB over a 184-house geocode run).
          if (status.updated_houses?.length) {
            this._mergeGeocodeDelta(status.updated_houses);
          } else {
            const d = await api('GET', `/users/${this.username}/sessions/${this.currentSession.id}`);
            this.setHouses(d.houses || []);
          }
        }
        return;
      }
      clearInterval(this._geocodePoll);
      this._geocodePoll = null;
      this._geocodeRunId = null;
      if (status.status === 'done') {
        if (status.updated_houses?.length) {
          this._mergeGeocodeDelta(status.updated_houses);
        } else {
          const d = await api('GET', `/users/${this.username}/sessions/${this.currentSession.id}`);
          this.setHouses(d.houses || []);
        }
        this.geocodeStatus = null;
        // Remember how many houses are still ungeocoded so reopen doesn't re-POST.
        this._geocodeAttemptedSession = this.currentSession?.id;
        this._geocodeAttemptedCount = this.houses.filter(h => h.lat == null && (h.manual_address || h.address)).length;
      }
      if (this.mapView) await this.initMap();
    } catch (e) {
      clearInterval(this._geocodePoll);
      this._geocodePoll = null;
      this._geocodeRunId = null;
      if (this.mapView) await this.initMap();
    }
  },

  /** Apply a run's geocode delta (internal_id/lat/lng/geocode_failed) in place. */
  _mergeGeocodeDelta(updated) {
    const byId = new Map(updated.map(u => [u.internal_id, u]));
    for (const h of this.houses) {
      const u = byId.get(h.internal_id);
      if (!u) continue;
      h.lat = u.lat;
      h.lng = u.lng;
      h.geocode_failed = u.geocode_failed;
    }
    this._invalidateCache();
  },
};

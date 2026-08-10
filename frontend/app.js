function app() {
  return {
    // ── Core state ───────────────────────────────────────────────────────────
    screen: 'login',
    username: '',
    sessions: [],
    currentSession: null,
    houses: [],
    loading: false,
    loadingMsg: '',
    modalType: null,
    newFilter: '',
    newLabel: '',
    newSources: [],
    newSourceEngine: 'argenprop',

    runId: null,
    runSessionId: null,
    runStatus: null,
    runDismissed: false,
    _pollInterval: null,
    filterReview: ['', 'en_duda', 'interesante', 'contactar', 'descartada'],
    filterStatus: ['active'],
    filterType: [],
    filterMinPrice: '',
    filterMaxPrice: '',
    filterAddress: '',
    filterRealEstate: '',
    filterPriceChange: false,
    filterNotes: '',
    filterProvider: [],
    filtersOpen: false,
    isNarrow: false,
    mobileInfoOpen: true,
    switchingView: false,
    _editSources: [],
    _editSourceEngine: 'argenprop',
    _editSourceFilter: '',
    sortBy: 'search_engine_id',
    sortDir: 'asc',
    detailHouse: null,
    lightboxImg: null,
    lightboxImages: [],
    lightboxIdx: 0,
    tablePage: 1,
    tablePageSize: 200,
    tooltip: { visible: false, text: '', x: 0, y: 0 },
    _tooltipTimer: null,
    notesSaved: false,
    toastMessage: '',
    toastType: '',
    confirmModal: { show: false, title: '', message: '', action: null },
    showShortcuts: false,
    editingLabel: false,
    editLabel: '',
    selectedHouses: [],
    compareHouses: [],
    visibleColumns: JSON.parse(localStorage.getItem('visibleColumns') || 'null') || {
      status: true, review: true, info: true, link: true, id: true, provider: true, type: true,
      address: true, price: true, m2: true, amb: true, dorm: true, expenses: true,
      realEstate: true, published: true, updated: true,
    },
    darkMode: false,
    actionsOpen: false,
    sessionsActionsOpen: false,
    showManualAddressModal: false,
    _savingAddressId: null,
    _savedAddressId: null,
    editingModalAddress: false,
    dedupGroups: [],
    dedupLoading: false,
    selectedDedupGroups: [],
    sameEngineDedupGroups: [],
    sameEngineDedupLoading: false,
    sameEngineDedupSelected: [],
    sameEngineDedupOpen: false,

    // ── Spread map state & methods ───────────────────────────────────────────
    ...mapMethods,

    // ── Computed ─────────────────────────────────────────────────────────────
    get isRunning() {
      return this.runStatus?.status === 'running';
    },

    get propertyTypes() {
      return [...new Set(this.houses.map(h => h.type).filter(Boolean))].sort();
    },

    get activeExtraFilters() {
      return [
        this.filterType.length > 0,
        this.filterStatus.length > 0,
        !!this.filterMinPrice,
        !!this.filterMaxPrice,
        !!this.filterAddress,
        !!this.filterRealEstate,
        this.filterProvider.length > 0,
      ].filter(Boolean).length;
    },

    get availableEngines() {
      const engines = new Set(this.houses.map(h => h.search_engine).filter(Boolean));
      for (const src of (this.currentSession?.search_sources || [])) {
        if (src.engine) engines.add(src.engine);
      }
      return [...engines].sort();
    },

    get dedupAllChecked() {
      return this.selectedDedupGroups.length > 0 && this.selectedDedupGroups.every(Boolean);
    },

    get sameEngineDedupAllChecked() {
      return this.sameEngineDedupSelected.length > 0 && this.sameEngineDedupSelected.every(Boolean);
    },

    _norm(s) {
      return (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    },

    get filteredHouses() {
      let list = [...this.houses];
      if (this.filterStatus.length > 0) list = list.filter(h => this.filterStatus.includes(h.status));
      if (this.filterType.length > 0) list = list.filter(h => this.filterType.includes(h.type));
      if (this.filterReview.length > 0) {
        list = list.filter(h => this.filterReview.includes(h.review || ''));
      }
      if (this.filterMinPrice) list = list.filter(h => h.price && h.price >= parseFloat(this.filterMinPrice));
      if (this.filterMaxPrice) list = list.filter(h => !h.price || h.price <= parseFloat(this.filterMaxPrice));
      if (this.filterAddress) {
        const tokens = this._norm(this.filterAddress).split(/\s+/).filter(Boolean);
        list = list.filter(h => {
          const addr = this._norm(h.manual_address || h.address);
          const addrTokens = addr.split(/\s+/).filter(Boolean);
          return tokens.every(qt => addrTokens.some(at => {
            if (qt.includes('%')) {
              const escaped = qt.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/%/g, '.*');
              return new RegExp('^' + escaped + '$').test(at);
            }
            return at.includes(qt);
          }));
        });
      }
      if (this.filterRealEstate) {
        const q = this._norm(this.filterRealEstate);
        list = list.filter(h => this._norm(h.real_estate).includes(q));
      }
      if (this.filterPriceChange) {
        list = list.filter(h => this.priceChangePct(h) !== null);
      }
      if (this.filterNotes) {
        const q = this._norm(this.filterNotes);
        list = list.filter(h => this._norm(h.notes || '').includes(q));
      }
      if (this.filterProvider.length > 0) {
        list = list.filter(h => this.filterProvider.includes(h.search_engine));
      }
      list.sort((a, b) => {
        let av = a[this.sortBy], bv = b[this.sortBy];
        if (av == null) av = this.sortDir === 'asc' ? Infinity : -Infinity;
        if (bv == null) bv = this.sortDir === 'asc' ? Infinity : -Infinity;
        if (typeof av === 'string') av = av.toLowerCase();
        if (typeof bv === 'string') bv = bv.toLowerCase();
        if (av < bv) return this.sortDir === 'asc' ? -1 : 1;
        if (av > bv) return this.sortDir === 'asc' ? 1 : -1;
        return 0;
      });
      return list;
    },

    get totalPages() {
      return Math.max(1, Math.ceil(this.filteredHouses.length / this.tablePageSize));
    },

    get pagedHouses() {
      const start = (this.tablePage - 1) * this.tablePageSize;
      return this.filteredHouses.slice(start, start + this.tablePageSize);
    },

    get pageRange() {
      const total = this.totalPages, cur = this.tablePage, delta = 2, pages = [];
      for (let p = Math.max(1, cur - delta); p <= Math.min(total, cur + delta); p++) pages.push(p);
      return pages;
    },

    get priceChangeSummary() {
      const up = this.houses.filter(h => { const p = this.priceChangePct(h); return p !== null && p > 0; }).length;
      const down = this.houses.filter(h => { const p = this.priceChangePct(h); return p !== null && p < 0; }).length;
      const parts = [];
      if (down > 0) parts.push(`${down} bajaron`);
      if (up > 0) parts.push(`${up} subieron`);
      return parts.length > 0 ? parts.join(', ') : null;
    },

    get sessionStats() {
      const active = this.houses.filter(h => h.status === 'active');
      const prices = active.map(h => h.price).filter(p => p != null && p > 0);
      const pricesM2 = active.map(h => h.price_per_m2).filter(p => p != null && p > 0);
      return {
        total: this.houses.length,
        active: active.length,
        removed: this.houses.length - active.length,
        priceMin: prices.length ? Math.min(...prices) : null,
        priceMax: prices.length ? Math.max(...prices) : null,
        priceMedian: prices.length ? prices.sort((a, b) => a - b)[Math.floor(prices.length / 2)] : null,
        avgM2: pricesM2.length ? Math.round(pricesM2.reduce((a, b) => a + b, 0) / pricesM2.length) : null,
      };
    },

    get manualAddressHouses() {
      function group(h) {
        const failed = !!h.geocode_failed;
        const hasManual = !!h.manual_address;
        if ( failed &&  hasManual) return 0;
        if ( failed && !hasManual) return 1;
        if (!failed &&  hasManual) return 2;
        return 3;
      }
      return [...this.houses].sort((a, b) => {
        const gd = group(a) - group(b);
        if (gd !== 0) return gd;
        return (String(a.search_engine_id || '')).localeCompare(String(b.search_engine_id || ''));
      });
    },

    // ── Helpers ──────────────────────────────────────────────────────────────
    reviewClass(review) {
      return REVIEW_CLASSES[review] || REVIEW_CLASSES[''];
    },

    detailChips(h) {
      if (!h) return [];
      const chips = [];
      if (h.ambientes != null)  chips.push({ icon: '🏠', label: 'Ambientes',   value: h.ambientes });
      if (h.dormitorios != null) chips.push({ icon: '🛏', label: 'Dormitorios', value: h.dormitorios });
      if (h.banos != null)       chips.push({ icon: '🚿', label: 'Baños',       value: h.banos });
      if (h.toilettes != null)   chips.push({ icon: '🪠', label: 'Toilettes',   value: h.toilettes });
      if (h.covered_m2)          chips.push({ icon: '📐', label: 'm² Cubiertos', value: h.covered_m2 + ' m²' });
      if (h.total_m2)            chips.push({ icon: '📏', label: 'm² Totales',   value: h.total_m2 + ' m²' });
      if (h.floor)               chips.push({ icon: '🏢', label: 'Piso',         value: h.floor });
      if (h.parking != null)     chips.push({ icon: '🚗', label: 'Cochera',      value: h.parking ? 'Sí' : 'No' });
      if (h.orientation)         chips.push({ icon: '🧭', label: 'Orientación',  value: h.orientation });
      return chips;
    },

    detailMeta(h) {
      if (!h) return [];
      const rows = [];
      if (h.condition)  rows.push(['Estado',      h.condition]);
      if (h.age_years != null) rows.push(['Antigüedad', h.age_years + ' años']);
      return rows;
    },

    showTooltip(event, text) {
      if (!text) return;
      clearTimeout(this._tooltipTimer);
      this._tooltipTimer = setTimeout(() => {
        const r = event.target.getBoundingClientRect();
        this.tooltip = { visible: true, text, x: r.left + r.width / 2, y: r.top - 6 };
      }, 500);
    },

    hideTooltip() {
      clearTimeout(this._tooltipTimer);
      this.tooltip.visible = false;
    },

    escHtml(str) {
      return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    },

    engineLabel(engine) {
      return ENGINE_LABELS[engine] || '';
    },

    previewUrl() {
      if (!this.newFilter) return '—';
      const bases = {
        zonaprop: 'https://www.zonaprop.com.ar',
        argenprop: 'https://www.argenprop.com',
        mercadolibre: 'https://inmuebles.mercadolibre.com.ar',
        remax: 'https://www.remax.com.ar/listings/buy',
      };
      const base = bases[this.newSourceEngine] || bases.zonaprop;
      const sep = this.newFilter.startsWith('/') || this.newFilter.startsWith('?') ? '' : '/';
      return base + sep + this.newFilter;
    },

    parseFilterUrl() {
      const val = this.newFilter.trim();
      if (!val.startsWith('http')) {
        this.newSourceEngine = '';
        return;
      }
      try {
        const u = new URL(val);
        const host = u.hostname.toLowerCase();
        if (host.includes('zonaprop.com.ar')) {
          this.newSourceEngine = 'zonaprop';
          this.newFilter = u.pathname + u.search;
        } else if (host.includes('argenprop.com')) {
          this.newSourceEngine = 'argenprop';
          this.newFilter = u.pathname + u.search;
        } else if (host.includes('mercadolibre.com.ar') || host.includes('inmuebles.mercadolibre')) {
          this.newSourceEngine = 'mercadolibre';
          this.newFilter = u.pathname + u.search;
        } else if (host.includes('remax.com.ar')) {
          this.newSourceEngine = 'remax';
          const match = u.pathname.match(/\/listings\/buy(.*)/);
          this.newFilter = (match ? match[1] : u.pathname) + u.search + u.hash;
        }
      } catch (e) { /* not a valid URL, ignore */ }
    },

    // ── Init / lifecycle ─────────────────────────────────────────────────────
    init() {
      const saved = localStorage.getItem('darkMode');
      this.darkMode = saved === 'true';
      if (this.darkMode) document.documentElement.classList.add('dark');

      window.addEventListener('beforeunload', (e) => {
        if (this.screen !== 'login') {
          e.preventDefault();
          e.returnValue = '';
        }
      });

      let _resizeTimer = null;
      const onResize = () => {
        clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(() => {
          if (!this.mapView) return;
          this._resizeMap();
          this._map?.invalidateSize({ animate: false });
        }, 150);
      };
      window.addEventListener('resize', onResize);
      window.visualViewport?.addEventListener('resize', onResize);

      const mq = window.matchMedia('(max-width: 1334px)');
      this.isNarrow = mq.matches;
      mq.addEventListener('change', (e) => { this.isNarrow = e.matches; });

      const redraw = () => {
        clearTimeout(this._redrawTimer);
        this._redrawTimer = setTimeout(() => {
          if (this.mapView && this._map) this.renderPins();
        }, 200);
      };
      ['filterStatus', 'filterType', 'filterReview', 'filterMaxPrice', 'filterAddress', 'filterRealEstate', 'houses'].forEach(k => {
        this.$watch(k, redraw);
      });
      this.$watch('showManualAddressModal', (val, old) => {
        if (!val && old && this.mapView && this._map) {
          this.$nextTick(() => this._map.invalidateSize({ animate: false }));
        }
      });
      this.$watch('mobileInfoOpen', () => {
        if (this.mapView && this._map) {
          this.$nextTick(() => { this._resizeMap(); this._map.invalidateSize({ animate: false }); });
        }
      });
    },

    // ── Navigation ───────────────────────────────────────────────────────────
    async setView(view) {
      const toMap = view === 'map';
      // Only no-op when clicking the active TABLE toggle. Never early-return for
      // 'map': viewInMap() re-calls setView('map') while already on the map to
      // re-trigger openMap()/initMap() and fly to the focused house.
      if (!toMap && !this.mapView) return;
      if (!toMap) {
        // The table remounts via x-if when mapView flips, which blocks the main
        // thread. Paint the spinner first, then flip the view, and destroy the
        // map so it doesn't keep running in the background (mirrors the table).
        this.switchingView = true;
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        this.mapView = false;
        this.destroyMap();
        await this.$nextTick();
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        this.switchingView = false;
        return;
      }
      // Already on the map (e.g. viewInMap re-focus from a pin's detail modal):
      // the map is alive, so just re-init without the spinner flash.
      if (this._map) {
        this._resizeMap();
        await this.openMap();
        return;
      }
      // Going to map: it was destroyed on the way out, so it must be re-created.
      // Show the spinner while the container mounts and the map initializes.
      this.switchingView = true;
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      this.mapView = true;
      await this.$nextTick();
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      this._resizeMap();
      try {
        await this.openMap();
      } finally {
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        this.switchingView = false;
      }
    },

    logout() {
      this.stopPolling();
      this.stopGeocoding();
      this.username = '';
      this.sessions = [];
      this.currentSession = null;
      this.houses = [];
      this.runId = null;
      this.runStatus = null;
      this.runDismissed = false;
      this.detailHouse = null;
      this.mapView = false;
      this.actionsOpen = false;
      this._mapFocusHouse = null;
      this.destroyMap();
      this._savedCenter = null;
      this._savedZoom = null;
      this.switchingView = false;
      this.screen = 'login';
    },

    async goBack() {
      this.loading = true; this.loadingMsg = 'Cargando...';
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      this.stopGeocoding();
      this.filterReview = ['', 'en_duda', 'interesante', 'contactar', 'descartada'];
      this.filterType = [];
      this.filterStatus = ['active'];
      this.filterMinPrice = '';
      this.filterMaxPrice = '';
      this.filterAddress = '';
      this.filterRealEstate = '';
      this.filtersOpen = false;
      this.mapView = false;
      this.destroyMap();
      this._savedCenter = null;
      this._savedZoom = null;
      try {
        const data = await api('GET', `/users/${this.username}`);
        this.sessions = data.sessions || [];
      } catch (e) { /* show stale list if fetch fails */ }
      this.screen = 'sessions';
      window.scrollTo(0, 0);
      try {
        await this.$nextTick();
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      } finally { this.loading = false; this.loadingMsg = ''; }
    },

    async login() {
      this.loading = true;
      try {
        const data = await api('GET', `/users/${this.username}`);
        if (data.is_new) {
          this.loading = false;
          this.showConfirm(
            'Crear usuario',
            `El usuario "${this.username}" no existe. ¿Querés crearlo?`,
            async () => {
              this.loading = true;
              this.sessions = data.sessions || [];
              this.screen = 'sessions';
              window.scrollTo(0, 0);
            }
          );
          return;
        }
        this.sessions = data.sessions || [];
        this.screen = 'sessions';
        window.scrollTo(0, 0);
      } catch (e) { this.showToast('Error al conectar con el servidor.', 'error'); }
      finally { this.loading = false; }
    },

    async selectSession(sessionId) {
      this.loading = true; this.loadingMsg = 'Cargando...';
      try {
        const data = await api('GET', `/users/${this.username}/sessions/${sessionId}`);
        this.currentSession = data;
        this.houses = data.houses || [];
        if (sessionId !== this.runSessionId) {
          this.runStatus = null; this.runDismissed = false;
          this.stopPolling();
        } else if (this.isRunning && !this._pollInterval) {
          this.startPolling();
        }
        this.tablePage = 1;
        this.filterReview = ['', 'en_duda', 'interesante', 'contactar', 'descartada'];
        this.filterType = [];
        this.filterStatus = ['active'];
        this.filterMinPrice = '';
        this.filterMaxPrice = '';
        this.filterAddress = '';
        this.filterRealEstate = '';
        this.filtersOpen = false;
        this.mapView = false;
        this.destroyMap();
        this._savedCenter = null;
        this._savedZoom = null;
        this.stopGeocoding();
        this.screen = 'session';
      } catch (e) { this.showToast('Error al cargar la búsqueda.', 'error'); }
      finally { this.loading = false; this.loadingMsg = ''; }
    },

    // ── Session CRUD ─────────────────────────────────────────────────────────
    async createSession() {
      if (this.newSources.length === 0) return;
      this.loading = true; this.loadingMsg = 'Creando búsqueda...';
      try {
        const session = await api('POST', `/users/${this.username}/sessions`, {
          search_sources: this.newSources,
          label: this.newLabel || null,
        });
        this.closeModal();
        await this.selectSession(session.id);
        await this.triggerRun();
      } catch (e) { this.showToast('Error al crear la búsqueda.', 'error'); }
      finally { this.loading = false; this.loadingMsg = ''; }
    },

    confirmDeleteSession(s) {
      this.showConfirm(
        'Eliminar búsqueda',
        `¿Eliminar la búsqueda "${s.label}" y todas sus propiedades? Esta acción no se puede deshacer.`,
        () => this.deleteSession(s.id)
      );
    },

    async deleteSession(sessionId) {
      this.loading = true;
      try {
        await api('DELETE', `/users/${this.username}/sessions/${sessionId}`);
        this.sessions = this.sessions.filter(s => s.id !== sessionId);
      } catch (e) { this.showToast('Error al eliminar la búsqueda.', 'error'); }
      finally { this.loading = false; }
    },

    // ── Run ──────────────────────────────────────────────────────────────────
    async triggerRun() {
      try {
        const data = await api('POST', `/users/${this.username}/sessions/${this.currentSession.id}/run`);
        this.runId = data.run_id; this.runSessionId = this.currentSession.id; this.runDismissed = false;
        this.runStatus = { status: 'running', message: 'Iniciando...', progress: 0, total: 0 };
        this.startPolling();
      } catch (e) { this.showToast('Error al iniciar la búsqueda.', 'error'); }
    },

    async dismissRun() {
      if (this.isRunning && this.runId) {
        try { await api('DELETE', `/runs/${this.runId}`); } catch (e) {}
        this.stopPolling();
      }
      this.runDismissed = true;
    },

    startPolling() {
      this.stopPolling();
      this._pollInterval = setInterval(() => this.pollRun(), 2000);
    },

    stopPolling() {
      if (this._pollInterval) { clearInterval(this._pollInterval); this._pollInterval = null; }
    },

    async pollRun() {
      if (!this.runId) return;
      try {
        this.runStatus = await api('GET', `/runs/${this.runId}`);
        if (this.runStatus.status !== 'running') {
          this.stopPolling();
          if (this.runStatus.status === 'done') {
            await this.selectSession(this.currentSession.id);
            // Auto-show same-engine dedup modal if reactivations detected
            const dedup = this.runStatus.same_engine_dedup;
            if (dedup && dedup.count > 0) {
              this.sameEngineDedupGroups = dedup.groups;
              this.sameEngineDedupSelected = dedup.groups.map(() => true);
              setTimeout(() => { this.sameEngineDedupOpen = true; }, 500);
            }
          }
        }
      } catch (e) { this.stopPolling(); }
    },

    // ── House actions ────────────────────────────────────────────────────────
    openDetail(h) { this.detailHouse = h; this.lightboxImg = null; this.notesSaved = false; },

    openDetailById(houseId) {
      const h = this.houses.find(h => h.internal_id === houseId);
      if (h) this.openDetail(h);
    },

    navigateDetail(dir) {
      if (!this.detailHouse || this.lightboxImg) return;
      const list = this.filteredHouses;
      const idx = list.findIndex(h => h.internal_id === this.detailHouse.internal_id);
      if (idx === -1) return;
      const next = list[idx + dir];
      if (next) this.openDetail(next);
    },

    async viewInMap(h) {
      this._focusActive = false;
      this._mapFocusHouse = h;
      await this.closeDetail();
      await this.setView('map');
    },

    async closeDetail() {
      if (this.detailHouse && this.$refs.notesTextarea) {
        await this.saveNotes(this.detailHouse, this.$refs.notesTextarea.value);
      }
      this.detailHouse = null;
      this.editingModalAddress = false;
      document.body.style.overflow = '';
    },

    openLightbox(images, idx) {
      this.lightboxImages = images;
      this.lightboxIdx = idx;
      this.lightboxImg = images[idx];
    },
    lightboxPrev() {
      if (!this.lightboxImg && !this.detailHouse) return;
      if (!this.lightboxImg && this.detailHouse?.images?.length) {
        this.openLightbox(this.detailHouse.images, this.detailHouse.images.length - 1);
        return;
      }
      if (!this.lightboxImages.length) return;
      this.lightboxIdx = (this.lightboxIdx - 1 + this.lightboxImages.length) % this.lightboxImages.length;
      this.lightboxImg = this.lightboxImages[this.lightboxIdx];
    },
    lightboxNext() {
      if (!this.lightboxImg && !this.detailHouse) return;
      if (!this.lightboxImg && this.detailHouse?.images?.length) {
        this.openLightbox(this.detailHouse.images, 0);
        return;
      }
      if (!this.lightboxImages.length) return;
      this.lightboxIdx = (this.lightboxIdx + 1) % this.lightboxImages.length;
      this.lightboxImg = this.lightboxImages[this.lightboxIdx];
    },

    async saveReview(h, value) {
      h.review = value;
      try { await api('PATCH', `/houses/${h.internal_id}`, { review: value }); } catch (e) { console.error('Error saving review', e); }
      if (this.mapView && this._markerMap) {
        this.updateMarkerColor(h);
      }
    },

    async saveNotes(h, value) {
      if (!h || value === (h.notes || '')) return;
      h.notes = value;
      try {
        await api('PATCH', `/houses/${h.internal_id}`, { notes: value });
        this.notesSaved = true;
        setTimeout(() => { this.notesSaved = false; }, 2000);
      } catch (e) { console.error('Error saving notes', e); }
    },

    async saveManualAddress(h, value) {
      const trimmed = value.trim();
      const effective = h.manual_address || h.address || '';
      if (trimmed === effective) return;
      if (!trimmed && !h.manual_address) return;
      h.manual_address = trimmed || null;
      h.lat = null;
      h.lng = null;
      h.geocode_failed = false;
      if (this._markerLayer) {
        const m = this._markerMap[h.internal_id];
        if (m) { this._markerLayer.removeLayer(m); delete this._markerMap[h.internal_id]; }
      }
      this._savingAddressId = h.internal_id;
      this._savedAddressId = null;
      try {
        await api('PATCH', `/houses/${h.internal_id}`, { manual_address: trimmed || null });
        this._savingAddressId = null;
        this._savedAddressId = h.internal_id;
        setTimeout(() => { if (this._savedAddressId === h.internal_id) this._savedAddressId = null; }, 2000);
        if (trimmed || h.address) {
          this._geocodeAfterAddressEdit();
          if (this.detailHouse?.internal_id === h.internal_id) {
            this._geocodeSingleHouseForModal(h.internal_id);
          }
        }
      } catch (e) {
        this._savingAddressId = null;
        console.error('Error saving manual address', e);
      }
    },

    async _geocodeSingleHouseForModal(houseId) {
      this._geocodingDetailHouseId = houseId;
      try {
        const data = await api('POST', `/houses/${houseId}/geocode`);
        if (data.already_done) { this._geocodingDetailHouseId = null; return; }
        const runId = data.run_id;
        const poll = async () => {
          try {
            const status = await api('GET', `/runs/${runId}`);
            if (status.status === 'running') {
              setTimeout(poll, 1500);
            } else {
              this._geocodingDetailHouseId = null;
              if (status.status === 'done' && this.detailHouse?.internal_id === houseId) {
                const updated = await api('GET', `/houses/${houseId}`);
                Object.assign(this.detailHouse, {
                  lat: updated.lat, lng: updated.lng, geocode_failed: updated.geocode_failed,
                });
                const idx = this.houses.findIndex(h => h.internal_id === houseId);
                if (idx !== -1) Object.assign(this.houses[idx], {
                  lat: updated.lat, lng: updated.lng, geocode_failed: updated.geocode_failed,
                });
              }
            }
          } catch (e) { this._geocodingDetailHouseId = null; }
        };
        setTimeout(poll, 1500);
      } catch (e) { this._geocodingDetailHouseId = null; }
    },

    async _geocodeAfterAddressEdit() {
      if (!this.currentSession) return;
      if (this._geocodeRunId) return;
      try {
        const res = await fetch(
          `/api/users/${this.username}/sessions/${this.currentSession.id}/geocode`,
          { method: 'POST' }
        );
        const data = await res.json();
        if (data.already_done) return;
        this._geocodeRunId = data.run_id;
        if (!this._geocodePoll) {
          this._geocodePoll = setInterval(() => this.pollGeocode(data.run_id), 2000);
        }
      } catch (e) { console.error('Error triggering geocode after address edit', e); }
    },

    async saveLabel() {
      this.editingLabel = false;
      const newLabel = (this.editLabel || '').trim();
      if (!newLabel || newLabel === (this.currentSession?.label || '')) return;
      try {
        await api('PUT', `/users/${this.username}/sessions/${this.currentSession.id}`, { label: newLabel });
        this.currentSession.label = newLabel;
        const s = this.sessions.find(s => s.id === this.currentSession.id);
        if (s) s.label = newLabel;
        this.showToast('Nombre actualizado', 'success');
      } catch (e) { this.showToast('Error al guardar el nombre.', 'error'); }
    },

    // ── Sources / Modal ──────────────────────────────────────────────────────
    openNewSearchModal() {
      this.closeModal();
      this.modalType = 'newSearch';
    },

    closeModal() {
      this.modalType = null;
      this.newFilter = '';
      this.newLabel = '';
      this.newSources = [];
      this.newSourceEngine = 'argenprop';
      this._editSourceFilter = '';
    },

    addNewSource() {
      if (!this.newFilter) return;
      const filter = this.newFilter.startsWith('/') ? this.newFilter : '/' + this.newFilter;
      this.newSources.push({ engine: this.newSourceEngine, filter });
      this.newFilter = '';
    },

    openSourceEditor() {
      this._editSources = (this.currentSession?.search_sources || []).map(s => ({ ...s }));
      this._editSourceFilter = '';
      this.modalType = 'sourceEditor';
    },

    addEditSource() {
      if (!this._editSourceFilter) return;
      const filter = this._editSourceFilter.startsWith('/') ? this._editSourceFilter : '/' + this._editSourceFilter;
      this._editSources.push({ engine: this._editSourceEngine, filter });
      this._editSourceFilter = '';
    },

    removeEditSource(idx) {
      if (this._editSources.length <= 1) {
        this.showToast('Debe quedar al menos una URL.', 'error');
        return;
      }
      this._editSources.splice(idx, 1);
    },

    async saveEditSources() {
      try {
        await api('PUT', `/users/${this.username}/sessions/${this.currentSession.id}`, { search_sources: this._editSources });
        this.currentSession.search_sources = this._editSources;
        this.closeModal();
        this.showToast('Fuentes actualizadas', 'success');
      } catch (e) { this.showToast('Error al guardar las fuentes.', 'error'); }
    },

    // ── Filters ──────────────────────────────────────────────────────────────
    toggleReviewFilter(val) {
      const i = this.filterReview.indexOf(val);
      if (i !== -1) this.filterReview = this.filterReview.filter(v => v !== val);
      else this.filterReview = [...this.filterReview, val];
      this.tablePage = 1;
    },

    toggleFilterType(val) {
      const i = this.filterType.indexOf(val);
      if (i !== -1) this.filterType = this.filterType.filter(v => v !== val);
      else this.filterType = [...this.filterType, val];
      this.tablePage = 1;
    },

    toggleFilterStatus(val) {
      const i = this.filterStatus.indexOf(val);
      if (i !== -1) this.filterStatus = this.filterStatus.filter(v => v !== val);
      else this.filterStatus = [...this.filterStatus, val];
      this.tablePage = 1;
    },

    toggleProviderFilter(val) {
      const i = this.filterProvider.indexOf(val);
      if (i !== -1) this.filterProvider = this.filterProvider.filter(v => v !== val);
      else this.filterProvider = [...this.filterProvider, val];
      this.tablePage = 1;
    },

    focusNextManualInput(current, direction) {
      const inputs = [...document.querySelectorAll('input[placeholder="Dirección alternativa…"]')];
      const idx = inputs.indexOf(current);
      const next = inputs[idx + direction];
      if (next) next.focus();
    },

    // ── Selection & Compare ──────────────────────────────────────────────────
    toggleSelectAll(event) {
      if (event.target.checked) {
        this.selectedHouses = this.pagedHouses.map(h => h.internal_id);
      } else {
        this.selectedHouses = [];
      }
    },

    toggleSelectHouse(id) {
      if (this.selectedHouses.includes(id)) {
        this.selectedHouses = this.selectedHouses.filter(i => i !== id);
      } else {
        this.selectedHouses.push(id);
      }
    },

    async batchSetReview(value) {
      const ids = [...this.selectedHouses];
      for (const id of ids) {
        const h = this.houses.find(h => h.internal_id === id);
        if (h) {
          h.review = value;
          try { await api('PATCH', `/houses/${id}`, { review: value }); } catch (e) { /* continue */ }
          if (this.mapView && this._markerMap) {
            this.updateMarkerColor(h);
          }
        }
      }
      this.selectedHouses = [];
      this.showToast(`${ids.length} propiedades actualizadas`, 'success');
    },

    openCompare() {
      if (this.selectedHouses.length < 2) {
        this.showToast('Seleccioná al menos 2 propiedades para comparar', 'error');
        return;
      }
      if (this.selectedHouses.length > 5) {
        this.showToast('Máximo 5 propiedades para comparar', 'error');
        return;
      }
      this.compareHouses = this.selectedHouses.map(id => this.houses.find(h => h.internal_id === id)).filter(Boolean);
      this.selectedHouses = [];
    },

    closeCompare() {
      this.compareHouses = [];
    },

    // ── Geocode actions ──────────────────────────────────────────────────────
    async forceGeocode() {
      if (this._geocodeRunId) return;
      try {
        const data = await api('POST', `/users/${this.username}/sessions/${this.currentSession.id}/geocode?force=true`);
        if (data.already_done) return;
        this._geocodeRunId = data.run_id;
        this.geocodeStatus = { status: 'running', message: 'Geocodificando…', progress: 0, total: 0 };
        this._geocodePoll = setInterval(() => this.pollGeocode(data.run_id), 2000);
        if (this.mapView) this.initMap();
      } catch (e) { this.showToast('Error al iniciar geocodificación.', 'error'); }
    },

    async clearGeodata() {
      this.showConfirm(
        'Borrar datos de geo',
        '¿Borrar los datos de ubicación (lat/lng) de todas las propiedades de esta búsqueda? Podrás volver a geocodificar abriendo el mapa.',
        async () => {
          try {
            await api('DELETE', `/users/${this.username}/sessions/${this.currentSession.id}/geodata`);
            this.houses.forEach(h => { h.lat = null; h.lng = null; h.geocode_failed = false; });
            if (this._markerLayer) this._markerLayer.clearLayers();
          } catch (e) { this.showToast('Error al borrar los datos de geo.', 'error'); }
        }
      );
    },

    // ── Deduplication ────────────────────────────────────────────────────────
    async openDedupModal() {
      this.dedupLoading = true;
      try {
        const data = await api('POST', `/users/${this.username}/sessions/${this.currentSession.id}/deduplicate/preview`);
        this.dedupGroups = data.groups || [];
        this.selectedDedupGroups = this.dedupGroups.map(() => true);
        if (this.dedupGroups.length === 0) {
          this.showToast('No se encontraron propiedades duplicadas.', 'info');
        }
      } catch (e) { this.showToast('Error al buscar duplicados.', 'error'); }
      finally { this.dedupLoading = false; }
    },

    toggleAllDedupGroups() {
      const v = !this.dedupAllChecked;
      this.selectedDedupGroups = this.selectedDedupGroups.map(() => v);
    },

    async confirmDedup() {
      const selected = this.selectedDedupGroups.reduce((acc, checked, i) => {
        if (checked) acc.push(i);
        return acc;
      }, []);
      if (selected.length === 0) {
        this.showToast('Seleccioná al menos un grupo para deduplicar.', 'info');
        return;
      }
      this.dedupLoading = true;
      try {
        const data = await api('POST', `/users/${this.username}/sessions/${this.currentSession.id}/deduplicate/apply`, { selected_groups: selected });
        this.dedupGroups = [];
        this.selectedDedupGroups = [];
        this.showToast(`${data.removed_count} duplicados marcados.`, 'success');
        await this.selectSession(this.currentSession.id);
      } catch (e) { this.showToast('Error al eliminar duplicados.', 'error'); }
      finally { this.dedupLoading = false; }
    },

    closeDedupModal() {
      this.dedupGroups = [];
      this.selectedDedupGroups = [];
    },

    // ── Same-engine dedup (reactivation detection) ─────────────────────────
    async openSameEngineDedupModal() {
      this.sameEngineDedupLoading = true;
      try {
        const data = await api('POST', `/users/${this.username}/sessions/${this.currentSession.id}/same-engine-dedup/preview`);
        this.sameEngineDedupGroups = data.groups || [];
        this.sameEngineDedupSelected = this.sameEngineDedupGroups.map(() => true);
        this.sameEngineDedupOpen = true;
        if (this.sameEngineDedupGroups.length === 0) {
          this.showToast('No se encontraron reactivaciones.', 'info');
          this.sameEngineDedupOpen = false;
        }
      } catch (e) { this.showToast('Error al buscar reactivaciones.', 'error'); }
      finally { this.sameEngineDedupLoading = false; }
    },

    toggleAllSameEngineDedup() {
      const v = !this.sameEngineDedupAllChecked;
      this.sameEngineDedupSelected = this.sameEngineDedupSelected.map(() => v);
    },

    toggleSameEngineDedupGroup(idx) {
      this.sameEngineDedupSelected[idx] = !this.sameEngineDedupSelected[idx];
    },

    async confirmSameEngineDedup() {
      const selected = this.sameEngineDedupSelected.reduce((acc, checked, i) => {
        if (checked) acc.push(i);
        return acc;
      }, []);
      if (selected.length === 0) {
        this.showToast('Seleccioná al menos un grupo para fusionar.', 'info');
        return;
      }
      this.sameEngineDedupLoading = true;
      try {
        const data = await api('POST', `/users/${this.username}/sessions/${this.currentSession.id}/same-engine-dedup/apply`, { selected_groups: selected });
        this.sameEngineDedupGroups = [];
        this.sameEngineDedupSelected = [];
        this.sameEngineDedupOpen = false;
        this.showToast(`${data.merged_count} reactivaciones fusionadas.`, 'success');
        await this.selectSession(this.currentSession.id);
      } catch (e) { this.showToast('Error al fusionar reactivaciones.', 'error'); }
      finally { this.sameEngineDedupLoading = false; }
    },

    closeSameEngineDedupModal() {
      this.sameEngineDedupGroups = [];
      this.sameEngineDedupSelected = [];
      this.sameEngineDedupOpen = false;
    },

    // ── Export / Import ──────────────────────────────────────────────────────
    exportCsv() {
      const FIELDS = [
        'internal_id', 'type', 'ambientes', 'dormitorios', 'banos', 'toilettes',
        'price', 'currency', 'price_per_m2', 'expenses', 'expenses_currency',
        'address', 'manual_address', 'covered_m2', 'total_m2', 'floor', 'parking',
        'orientation', 'age_years', 'condition', 'real_estate',
        'amenities', 'description', 'url', 'status', 'review', 'notes', 'last_updated', 'created_at',
      ];
      const escape = v => {
        if (v == null) return '';
        const s = Array.isArray(v) ? v.join(', ') : String(v);
        return s.includes(',') || s.includes('"') || s.includes('\n')
          ? '"' + s.replace(/"/g, '""') + '"'
          : s;
      };
      const rows = [FIELDS.join(',')];
      for (const h of this.filteredHouses) {
        rows.push(FIELDS.map(f => escape(h[f])).join(','));
      }
      const blob = new Blob(['\ufeff' + rows.join('\n')], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `casas_${this.currentSession.id.slice(0, 8)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    },

    async copyHouseLinks() {
      const lines = this.filteredHouses.map((h, i) => {
        const price = this.formatPrice(h.price, h.currency);
        return `* #${i + 1} ${price} ${h.url}`;
      });
      await navigator.clipboard.writeText(lines.join('\n'));
      this.showToast('¡Links copiados al portapapeles!');
    },

    importDb() {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,application/json';
      input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const formData = new FormData();
        formData.append('file', file);
        try {
          const res = await fetch('/api/admin/import-db', { method: 'POST', body: formData });
          const data = await res.json();
          if (!res.ok) {
            this.showToast(`Error al importar: ${data.detail}`, 'error');
            return;
          }
          this.showToast('Base de datos importada. Cerrando sesión…', 'success');
          this.logout();
        } catch (err) {
          this.showToast('Error de red al importar la base de datos.', 'error');
        }
      };
      input.click();
    },

    // ── Sorting ──────────────────────────────────────────────────────────────
    setSort(col) {
      if (this.sortBy === col) { this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc'; }
      else { this.sortBy = col; this.sortDir = 'asc'; }
      this.tablePage = 1;
    },

    sortIndicator(col) {
      if (this.sortBy !== col) return '';
      return this.sortDir === 'asc' ? '\u2191' : '\u2193';
    },

    // ── Formatting ───────────────────────────────────────────────────────────
    priceChangePct(h) {
      if (!h.previous_prices || h.previous_prices.length === 0) return null;
      const first = h.previous_prices[0];
      if (!h.price || !first || !first.price) return null;
      if (h.currency !== first.currency) return null;
      const pct = ((h.price - first.price) / first.price) * 100;
      if (Math.abs(pct) < 0.5) return null;
      return pct;
    },

    formatPrice(price, currency) {
      if (price == null) return '\u2014';
      const formatted = new Intl.NumberFormat('es-AR').format(Math.round(price));
      return currency === 'USD' ? `USD ${formatted}` : `$ ${formatted}`;
    },

    formatDate(iso) {
      if (!iso) return '\u2014';
      return new Date(iso).toLocaleString('es-AR', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    },

    formatDateOnly(iso) {
      if (!iso) return '\u2014';
      return new Date(iso).toLocaleDateString('es-AR', {
        day: '2-digit', month: '2-digit', year: 'numeric',
      });
    },

    timeAgo(iso) {
      if (!iso) return '';
      const diff = Date.now() - new Date(iso).getTime();
      const mins = Math.floor(diff / 60000);
      if (mins < 1) return 'ahora';
      if (mins < 60) return `hace ${mins}m`;
      const hours = Math.floor(mins / 60);
      if (hours < 24) return `hace ${hours}h`;
      const days = Math.floor(hours / 24);
      if (days < 7) return `hace ${days}d`;
      return new Date(iso).toLocaleDateString('es-AR');
    },

    // ── UI ───────────────────────────────────────────────────────────────────
    showToast(msg, type = 'info') {
      this.toastMessage = msg;
      this.toastType = type;
      setTimeout(() => { this.toastMessage = ''; this.toastType = ''; }, 3000);
    },

    showConfirm(title, message, action) {
      this.confirmModal = { show: true, title, message, action };
    },

    handleConfirm() {
      const { action } = this.confirmModal;
      this.confirmModal = { show: false, title: '', message: '', action: null };
      if (action) action();
    },

    cancelConfirm() {
      this.confirmModal = { show: false, title: '', message: '', action: null };
    },

    toggleDark() {
      this.darkMode = !this.darkMode;
      document.documentElement.classList.toggle('dark', this.darkMode);
      localStorage.setItem('darkMode', this.darkMode);
    },
  };
}

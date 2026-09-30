(function () {
  const { createApp } = Vue;

  function authHeaders(json = true) {
    const token = localStorage.getItem("aienglish_token");
    const headers = {};
    if (json) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = "Bearer " + token;
    return headers;
  }

  function currentUser() {
    try {
      return JSON.parse(localStorage.getItem("aienglish_user") || "null");
    } catch {
      return null;
    }
  }

  createApp({
    data() {
      return {
        user: currentUser(),
        tab: "assignments",
        msg: "",
        error: "",
        loading: false,
        assignments: [],
        students: [],
        classes: [],
        classDetail: null,
        words: [],
        questions: [],
        videoResources: [],
        selected: null,
        submissions: [],
        pronStatus: null,
        pronHistory: [],
        pronResult: null,
        classForm: { name: "", description: "" },
        joinCode: "",
        addStudentIds: [],
        createForm: {
          title: "",
          description: "",
          due_at: "",
          class_id: "",
          assign_all_students: false,
          student_ids: [],
          publish: true,
          includeChoice: true,
          includeVideo: false,
          includeListen: false,
          includeVocab: true,
          includeDictation: false,
          includeVoice: true,
          question_ids: [],
          word_ids: [],
          resource_filename: "",
          listen_filename: "",
          min_seconds: 10,
          voice_prompt: "Please read aloud: Hello! Nice to meet you. My name is Tom.",
          pass_score: 50,
        },
        practiceIndex: 0,
        flipped: false,
        choiceAnswers: {},
        videoWatchedByTask: {},
        mediaSessions: {},
        mediaLastBeat: {},
        dictationText: {},
        dashboard: null,
        wrongbook: [],
        textbooks: [],
        textbookForm: { title: "", level: "", description: "" },
        chapterForm: { textbook_id: "", title: "", objectives: "", word_ids: [], question_ids: [], resource_filenames: [] },
        bindBookForm: { class_id: "", textbook_id: "" },
        assignChapterForm: { chapter_id: "", class_id: "", include_dictation: true, include_voice: false, due_days: 3 },
        selectedTextbook: null,
        chapters: [],
        editDueAt: "",
        recording: false,
        recorder: null,
        chunks: [],
        recordSeconds: 0,
        recordTimer: null,
        recordTarget: null,
        recordTaskId: null,
        practiceText: "Hello! Nice to meet you. My name is Tom.",
        reviewForms: {},
        liveTranscript: "",
        recognition: null,
        parentInvite: "",
        linkedParents: [],
        bindCode: "",
        children: [],
        childOverview: null,
        inboxItems: [],
        inboxUnread: 0,
        notifyChannels: null,
        channelForm: { wechat_openid: "", notify_webhook: "" },
        inboxTimer: null,
        hourBalance: null,
        lessons: [],
        tutorSlots: [],
        contracts: [],
        hourForm: { student_id: "", hours: 10, note: "" },
        lessonForm: { class_id: "", title: "", starts_at: "", hours_cost: 1 },
        slotForm: { starts_at: "", ends_at: "", topic: "口语陪练" },
        contractForm: {
          student_id: "", title: "季度课时包", total_amount: 0,
          hours_included: 20, activate: true,
        },
        paymentForm: { contract_id: "", amount: 0, hours_granted: 0, method: "transfer", note: "" },
        reopenResetSubmitted: false,
        quickWord: { word: "", meaning: "", phonetic: "", example: "" },
        quickQuestion: { stem: "", option_a: "", option_b: "", option_c: "", option_d: "", answer: "A", explanation: "" },
      };
    },
    computed: {
      isTeacher() {
        return this.user && (this.user.role === "teacher" || this.user.role === "admin");
      },
      isStudent() {
        return this.user && this.user.role === "student";
      },
      isParent() {
        return this.user && this.user.role === "parent";
      },
      currentWord() {
        if (!this.words.length) return null;
        return this.words[this.practiceIndex % this.words.length];
      },
      canEditAssignment() {
        if (!this.selected) return false;
        if (this.selected.accepting_answers === false) return false;
        const sub = this.selected.submission;
        return !sub || sub.status === "in_progress";
      },
    },
    mounted() {
      if (!this.user || !localStorage.getItem("aienglish_token")) {
        this.error = "请先在右上角登录，再使用学习工作台。";
        return;
      }
      if (this.isParent) {
        this.tab = "parent";
        this.refreshParent();
      } else {
        this.refresh();
      }
      this.loadInbox();
      this.inboxTimer = setInterval(() => this.loadInbox(true), 45000);
    },
    unmounted() {
      if (this.inboxTimer) clearInterval(this.inboxTimer);
    },
    methods: {
      async ok(r) {
        if (r.status === 401) {
          localStorage.removeItem("aienglish_token");
          localStorage.removeItem("aienglish_user");
          throw Error("登录已过期，请重新登录");
        }
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
          const detail = d.detail;
          const msg = typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((x) => x.msg || JSON.stringify(x)).join("; ")
              : JSON.stringify(detail || r.status);
          throw Error(msg);
        }
        return d;
      },
      async refresh() {
        this.loading = true;
        this.error = "";
        try {
          const [list, classes, pronStatus] = await Promise.all([
            fetch("/api/assignments", { headers: authHeaders(false) }).then(this.ok),
            fetch("/api/classes", { headers: authHeaders(false) }).then(this.ok),
            fetch("/api/pronunciation/status", { headers: authHeaders(false) }).then(this.ok),
          ]);
          this.assignments = list.items || [];
          this.classes = classes.items || [];
          this.pronStatus = pronStatus;
          if (this.isTeacher) {
            const [students, words, questions, resources] = await Promise.all([
              fetch("/api/students", { headers: authHeaders(false) }).then(this.ok),
              fetch("/api/words", { headers: authHeaders(false) }).then(this.ok),
              fetch("/api/questions", { headers: authHeaders(false) }).then(this.ok),
              fetch("/api/resources?kind=mp4").then(this.ok),
            ]);
            this.students = students.items || [];
            this.words = words.items || [];
            this.questions = questions.items || [];
            this.videoResources = (resources.items || []).filter((x) => !x.duplicate_of);
            if (!this.createForm.question_ids.length) this.createForm.question_ids = this.questions.slice(0, 3).map((q) => q.id);
            if (!this.createForm.word_ids.length) this.createForm.word_ids = this.words.slice(0, 5).map((w) => w.id);
            if (!this.createForm.resource_filename && this.videoResources.length) {
              this.createForm.resource_filename = this.videoResources[0].filename;
            }
            if (!this.createForm.listen_filename && this.videoResources.length) {
              this.createForm.listen_filename = this.videoResources[0].filename;
            }
            if (!this.createForm.class_id && this.classes.length) this.createForm.class_id = this.classes[0].id;
            await this.loadDashboard(true);
          }
          const history = await fetch("/api/pronunciation/history", { headers: authHeaders(false) }).then(this.ok);
          this.pronHistory = history.items || [];
          if (this.isStudent) {
            const [vocab, invite] = await Promise.all([
              fetch("/api/my/vocab", { headers: authHeaders(false) }).then(this.ok),
              fetch("/api/students/me/parent-invite", { headers: authHeaders(false) }).then(this.ok),
            ]);
            this.words = vocab.items || [];
            this.parentInvite = invite.invite_code || "";
            this.linkedParents = invite.parents || [];
            await this.loadWrongbook(true);
          }
          await this.loadInbox(true);
        } catch (e) {
          this.error = e.message || "加载失败";
        } finally {
          this.loading = false;
        }
      },
      async refreshParent() {
        this.loading = true;
        this.error = "";
        try {
          const [children, pronStatus] = await Promise.all([
            fetch("/api/parents/children", { headers: authHeaders(false) }).then(this.ok),
            fetch("/api/pronunciation/status", { headers: authHeaders(false) }).then(this.ok),
          ]);
          this.children = children.items || [];
          this.pronStatus = pronStatus;
          await this.loadInbox(true);
        } catch (e) {
          this.error = e.message || "加载失败";
        } finally {
          this.loading = false;
        }
      },
      async loadInbox(silent = false) {
        if (!localStorage.getItem("aienglish_token")) return;
        try {
          const data = await fetch("/api/notifications", { headers: authHeaders(false) }).then(this.ok);
          this.inboxItems = data.items || [];
          this.inboxUnread = data.unread || 0;
          this.notifyChannels = data.channels || null;
          if (data.user_channels) {
            this.channelForm = {
              wechat_openid: data.user_channels.wechat_openid || "",
              notify_webhook: data.user_channels.notify_webhook || "",
            };
          }
        } catch (e) {
          if (!silent) this.error = e.message;
        }
      },
      async markRead(n) {
        if (!n || n.is_read) return;
        try {
          await fetch(`/api/notifications/${n.id}/read`, { method: "POST", headers: authHeaders() }).then(this.ok);
          n.is_read = 1;
          this.inboxUnread = Math.max(0, this.inboxUnread - 1);
        } catch (e) {
          this.error = e.message;
        }
      },
      async markAllRead() {
        try {
          await fetch("/api/notifications/read-all", { method: "POST", headers: authHeaders() }).then(this.ok);
          this.inboxItems.forEach((x) => { x.is_read = 1; });
          this.inboxUnread = 0;
          this.msg = "已全部标为已读";
        } catch (e) {
          this.error = e.message;
        }
      },
      async saveChannels() {
        try {
          const data = await fetch("/api/notifications/channels", {
            method: "PUT",
            headers: authHeaders(),
            body: JSON.stringify(this.channelForm),
          }).then(this.ok);
          this.notifyChannels = data.channels || this.notifyChannels;
          this.msg = "推送渠道已保存";
        } catch (e) {
          this.error = e.message;
        }
      },
      async loadDashboard(silent = false) {
        if (!this.isTeacher) return;
        try {
          this.dashboard = await fetch("/api/teacher/dashboard", { headers: authHeaders(false) }).then(this.ok);
        } catch (e) {
          if (!silent) this.error = e.message;
        }
      },
      async loadOps(silent = false) {
        try {
          const tasks = [
            fetch("/api/tutor/slots", { headers: authHeaders(false) }).then(this.ok),
            fetch("/api/contracts", { headers: authHeaders(false) }).then(this.ok),
          ];
          if (this.isStudent) {
            tasks.push(fetch("/api/hours/me", { headers: authHeaders(false) }).then(this.ok));
          } else if (this.isTeacher) {
            tasks.push(fetch("/api/lessons", { headers: authHeaders(false) }).then(this.ok));
            if (!this.students.length) {
              const st = await fetch("/api/students", { headers: authHeaders(false) }).then(this.ok);
              this.students = st.items || [];
            }
            if (!this.classes.length) {
              const cl = await fetch("/api/classes", { headers: authHeaders(false) }).then(this.ok);
              this.classes = cl.items || [];
            }
            if (!this.lessonForm.class_id && this.classes.length) this.lessonForm.class_id = this.classes[0].id;
          } else if (this.isParent) {
            if (!this.children.length) {
              const ch = await fetch("/api/parents/children", { headers: authHeaders(false) }).then(this.ok);
              this.children = ch.items || [];
            }
            if (this.children[0]) {
              tasks.push(
                fetch(`/api/hours/students/${this.children[0].id}`, { headers: authHeaders(false) }).then(this.ok),
              );
            }
          }
          const results = await Promise.all(tasks);
          this.tutorSlots = (results[0].items || []).slice(0, 40);
          this.contracts = results[1].items || [];
          if (this.isTeacher) this.lessons = (results[2].items || []).slice(0, 30);
          else if (results[2]) this.hourBalance = results[2];
          if (this.isTeacher && this.hourForm.student_id) {
            try {
              this.hourBalance = await fetch(`/api/hours/students/${this.hourForm.student_id}`, {
                headers: authHeaders(false),
              }).then(this.ok);
            } catch {}
          }
        } catch (e) {
          if (!silent) this.error = e.message;
        }
      },
      async purchaseHours() {
        if (!this.hourForm.student_id) { this.error = "请选择学员"; return; }
        try {
          const r = await fetch("/api/hours/purchase", {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({
              student_id: Number(this.hourForm.student_id),
              hours: Number(this.hourForm.hours) || 0,
              note: this.hourForm.note || null,
            }),
          }).then(this.ok);
          this.msg = `已充值，剩余 ${r.remain_hours} 课时`;
          this.hourBalance = r;
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async createLesson() {
        try {
          await fetch("/api/lessons", {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({
              class_id: Number(this.lessonForm.class_id),
              title: this.lessonForm.title.trim() || "班级课",
              starts_at: this.lessonForm.starts_at,
              hours_cost: Number(this.lessonForm.hours_cost) || 1,
              duration_minutes: 60,
            }),
          }).then(this.ok);
          this.msg = "课程已创建";
          this.lessonForm.title = "";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async markAllPresent(lesson) {
        try {
          const detail = await fetch(`/api/classes/${lesson.class_id}`, { headers: authHeaders(false) }).then(this.ok);
          const items = (detail.members || []).map((m) => ({ student_id: m.id, status: "present" }));
          if (!items.length) { this.error = "班级无学员"; return; }
          const r = await fetch(`/api/lessons/${lesson.id}/attendance`, {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({ items }),
          }).then(this.ok);
          this.msg = `已点名 ${r.marked} 人，新扣费 ${r.newly_deducted}`;
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async cancelLesson(lesson) {
        try {
          await fetch(`/api/lessons/${lesson.id}`, {
            method: "PUT", headers: authHeaders(),
            body: JSON.stringify({ status: "cancelled" }),
          }).then(this.ok);
          this.msg = "课程已取消，已出席课时已退还";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async createSlot() {
        try {
          await fetch("/api/tutor/slots", {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({
              slots: [{
                starts_at: this.slotForm.starts_at,
                ends_at: this.slotForm.ends_at,
                topic: this.slotForm.topic || null,
              }],
            }),
          }).then(this.ok);
          this.msg = "口语时段已发布";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async bookSlot(slot) {
        try {
          await fetch(`/api/tutor/slots/${slot.id}/book`, {
            method: "POST", headers: authHeaders(), body: "{}",
          }).then(this.ok);
          this.msg = "预约成功";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async cancelBooking(id) {
        try {
          await fetch(`/api/tutor/bookings/${id}/cancel`, {
            method: "POST", headers: authHeaders(), body: "{}",
          }).then(this.ok);
          this.msg = "已取消预约";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async cancelSlot(slot) {
        try {
          await fetch(`/api/tutor/slots/${slot.id}/cancel`, {
            method: "POST", headers: authHeaders(), body: "{}",
          }).then(this.ok);
          this.msg = "时段已取消";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async completeBooking(slot) {
        if (!slot.booking_id) { this.error = "无预约记录"; return; }
        try {
          const r = await fetch(`/api/tutor/bookings/${slot.booking_id}/complete`, {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({ hours_cost: 1 }),
          }).then(this.ok);
          this.msg = `已完成，扣 ${r.hours_cost} 课时`;
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async createContract() {
        if (!this.contractForm.student_id) { this.error = "请选择学员"; return; }
        try {
          await fetch("/api/contracts", {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({
              student_id: Number(this.contractForm.student_id),
              title: this.contractForm.title.trim() || "课时包",
              total_amount: Number(this.contractForm.total_amount) || 0,
              hours_included: Number(this.contractForm.hours_included) || 0,
              activate: !!this.contractForm.activate,
              grant_hours_on_activate: true,
            }),
          }).then(this.ok);
          this.msg = "合同已创建";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async cancelContract(c) {
        try {
          const r = await fetch(`/api/contracts/${c.id}/cancel`, {
            method: "POST", headers: authHeaders(), body: "{}",
          }).then(this.ok);
          this.msg = r.hours_clawed_back
            ? `合同已作废，冲回 ${r.hours_clawed_back} 课时`
            : "合同已作废";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      async recordPayment() {
        const cid = Number(this.paymentForm.contract_id);
        if (!cid) { this.error = "请选择合同"; return; }
        if (!(Number(this.paymentForm.amount) > 0)) { this.error = "请填写到账金额"; return; }
        try {
          const r = await fetch(`/api/contracts/${cid}/payments`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({
              amount: Number(this.paymentForm.amount),
              hours_granted: Number(this.paymentForm.hours_granted) || 0,
              method: this.paymentForm.method || "transfer",
              note: this.paymentForm.note || null,
            }),
          }).then(this.ok);
          this.msg = `已登记缴费 ${r.amount}，入账课时 ${r.hours_granted}`;
          this.paymentForm.amount = 0;
          this.paymentForm.hours_granted = 0;
          this.paymentForm.note = "";
          await this.loadOps(true);
        } catch (e) { this.error = e.message; }
      },
      pickContractForPay(c) {
        this.paymentForm.contract_id = c.id;
        const remainAmt = Math.max(0, Number(c.total_amount || 0) - Number(c.paid_amount || 0));
        const remainHrs = Math.max(0, Number(c.hours_included || 0) - Number(c.hours_granted || 0));
        this.paymentForm.amount = remainAmt;
        this.paymentForm.hours_granted = remainHrs;
        this.msg = `已选合同 #${c.id}，默认填入剩余应付`;
      },
      async loadWrongbook(silent = false) {
        if (!this.isStudent) return;
        try {
          const d = await fetch("/api/my/wrongbook", { headers: authHeaders(false) }).then(this.ok);
          this.wrongbook = d.items || [];
        } catch (e) {
          if (!silent) this.error = e.message;
        }
      },
      async ensureMediaSession(task) {
        if (this.mediaSessions[task.id]) return this.mediaSessions[task.id];
        try {
          const mediaKey = task.config.resource_filename || task.video_url || task.audio_url || "media";
          const d = await fetch("/api/media/session/start", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({
              assignment_id: this.selected.id,
              task_id: task.id,
              media_key: mediaKey,
              duration_seconds: 0,
            }),
          }).then(this.ok);
          this.mediaSessions[task.id] = { session_id: d.session_id, token: d.token };
          this.mediaLastBeat[task.id] = 0;
          return this.mediaSessions[task.id];
        } catch (e) {
          this.error = "媒体进度会话启动失败：" + e.message;
          throw e;
        }
      },
      async beatMedia(task, position, delta) {
        try {
          const sess = await this.ensureMediaSession(task);
          const r = await fetch("/api/media/session/heartbeat", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({
              session_id: sess.session_id,
              token: sess.token,
              position: Number(position) || 0,
              delta: Math.max(0, Number(delta) || 0),
            }),
          }).then(this.ok);
          const eligible = Number(r.eligible_seconds || 0);
          this.videoWatchedByTask[task.id] = Math.max(this.watchedOf(task.id), eligible);
          return r;
        } catch (e) {
          this.error = e.message;
          return null;
        }
      },
      async bindChild() {
        try {
          const r = await fetch("/api/parents/bind", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ invite_code: this.bindCode }),
          }).then(this.ok);
          this.msg = "已绑定子女：" + r.display_name;
          this.bindCode = "";
          await this.refreshParent();
        } catch (e) {
          this.error = e.message;
        }
      },
      async openChild(child) {
        try {
          this.childOverview = await fetch(`/api/parents/children/${child.id}/overview`, {
            headers: authHeaders(false),
          }).then(this.ok);
          this.tab = "childDetail";
        } catch (e) {
          this.error = e.message;
        }
      },
      async unbindChild(id) {
        try {
          await fetch(`/api/parents/children/${id}/unbind`, {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          this.msg = "已解除绑定";
          this.childOverview = null;
          this.tab = "parent";
          await this.refreshParent();
        } catch (e) {
          this.error = e.message;
        }
      },
      async rotateParentInvite() {
        try {
          const r = await fetch("/api/students/me/parent-invite/rotate", {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          this.parentInvite = r.invite_code;
          this.msg = "家长邀请码已更新";
        } catch (e) {
          this.error = e.message;
        }
      },
      async revokeParent(pid) {
        try {
          const r = await fetch(`/api/students/me/parents/${pid}/revoke`, {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          if (r.invite_code) this.parentInvite = r.invite_code;
          this.msg = "已解除家长绑定，邀请码已轮换";
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async urgeAssignment(item) {
        try {
          const r = await fetch(`/api/assignments/${item.id}/urge`, {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          this.msg = `已催交未完成学员 ${r.urged_students} 人`;
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      startSpeechRecognition() {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        this.liveTranscript = "";
        if (!SR) return;
        try {
          this.recognition = new SR();
          this.recognition.lang = "en-US";
          this.recognition.continuous = true;
          this.recognition.interimResults = true;
          this.recognition.onresult = (e) => {
            let text = "";
            for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript + " ";
            this.liveTranscript = text.trim();
          };
          this.recognition.onerror = () => {};
          this.recognition.start();
        } catch {
          this.recognition = null;
        }
      },
      stopSpeechRecognition() {
        if (this.recognition) {
          try {
            this.recognition.stop();
          } catch {}
          this.recognition = null;
        }
      },
      async openAssignment(item) {
        this.msg = "";
        this.error = "";
        this.choiceAnswers = {};
        this.videoWatchedByTask = {};
        this.submissions = [];
        this.reviewForms = {};
        this.mediaSessions = {};
        this.mediaLastBeat = {};
        try {
          this.selected = await fetch("/api/assignments/" + item.id, { headers: authHeaders(false) }).then(this.ok);
          if (this.isTeacher) {
            const subs = await fetch("/api/assignments/" + item.id + "/submissions", { headers: authHeaders(false) }).then(this.ok);
            this.submissions = subs.items || [];
            const forms = {};
            for (const sub of this.submissions) {
              forms[sub.id] = { score: sub.score ?? 0, teacher_comment: sub.teacher_comment || "" };
            }
            this.reviewForms = forms;
            this.tab = "detail";
          } else {
            if (this.selected.submission && this.selected.submission.answers) {
              for (const a of this.selected.submission.answers) {
                if (a.answer && a.answer.answers) {
                  for (const [k, v] of Object.entries(a.answer.answers)) {
                    this.choiceAnswers[k] = v;
                    const n = Number(k);
                    if (!Number.isNaN(n)) this.choiceAnswers[n] = v;
                  }
                }
                if (a.answer && a.answer.watched_seconds != null) {
                  this.videoWatchedByTask[a.task_id] = Number(a.answer.watched_seconds) || 0;
                }
                if (a.answer && a.answer.text) {
                  this.dictationText[a.task_id] = a.answer.text;
                }
              }
            }
            this.tab = "detail";
          }
        } catch (e) {
          this.error = e.message;
        }
      },
      watchedOf(taskId) {
        return Number(this.videoWatchedByTask[taskId] || 0);
      },
      taskAnswer(taskId) {
        const answers = (this.selected && this.selected.submission && this.selected.submission.answers) || [];
        return answers.find((a) => a.task_id === taskId) || null;
      },
      assessmentOf(taskId) {
        const ans = this.taskAnswer(taskId);
        return (ans && ans.answer && ans.answer.assessment) || null;
      },
      async createClass() {
        try {
          const r = await fetch("/api/classes", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify(this.classForm),
          }).then(this.ok);
          this.msg = `班级已创建，邀请码 ${r.invite_code}`;
          this.classForm = { name: "", description: "" };
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async joinClass() {
        try {
          const r = await fetch("/api/classes/join", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ invite_code: this.joinCode }),
          }).then(this.ok);
          this.msg = `已加入班级：${r.name}`;
          this.joinCode = "";
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async openClass(item) {
        try {
          this.classDetail = await fetch("/api/classes/" + item.id, { headers: authHeaders(false) }).then(this.ok);
          this.addStudentIds = [];
          this.tab = "classDetail";
        } catch (e) {
          this.error = e.message;
        }
      },
      async addMembers() {
        if (!this.classDetail || !this.addStudentIds.length) return;
        try {
          const r = await fetch(`/api/classes/${this.classDetail.id}/members`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ student_ids: this.addStudentIds.map(Number) }),
          }).then(this.ok);
          this.msg = `已添加 ${r.added} 名学员`;
          await this.openClass(this.classDetail);
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async removeMember(sid) {
        try {
          await fetch(`/api/classes/${this.classDetail.id}/members/remove`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ student_ids: [sid] }),
          }).then(this.ok);
          this.msg = "已移除学员";
          await this.openClass(this.classDetail);
        } catch (e) {
          this.error = e.message;
        }
      },
      async regenInvite() {
        try {
          const r = await fetch(`/api/classes/${this.classDetail.id}/regenerate-invite`, {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          this.classDetail.invite_code = r.invite_code;
          this.msg = "邀请码已更新：" + r.invite_code;
        } catch (e) {
          this.error = e.message;
        }
      },
      async createAssignment() {
        this.msg = "";
        this.error = "";
        const tasks = [];
        if (this.createForm.includeChoice && this.createForm.question_ids.length) {
          tasks.push({
            task_type: "choice",
            title: "选择题练习",
            config: { question_ids: this.createForm.question_ids.map(Number) },
          });
        }
        if (this.createForm.includeVideo && this.createForm.resource_filename) {
          tasks.push({
            task_type: "video",
            title: "看视频学习",
            config: {
              resource_filename: this.createForm.resource_filename,
              min_seconds: Number(this.createForm.min_seconds) || 10,
            },
          });
        }
        if (this.createForm.includeListen && this.createForm.listen_filename) {
          tasks.push({
            task_type: "listen",
            title: "听力跟听",
            config: {
              resource_filename: this.createForm.listen_filename,
              min_seconds: Number(this.createForm.min_seconds) || 10,
            },
          });
        }
        if (this.createForm.includeVocab && this.createForm.word_ids.length) {
          tasks.push({
            task_type: "vocab",
            title: "背单词",
            config: { word_ids: this.createForm.word_ids.map(Number) },
          });
        }
        if (this.createForm.includeDictation && this.createForm.word_ids.length) {
          tasks.push({
            task_type: "dictation",
            title: "单词听写",
            config: { word_ids: this.createForm.word_ids.map(Number) },
          });
        }
        if (this.createForm.includeVoice && this.createForm.voice_prompt.trim()) {
          tasks.push({
            task_type: "voice",
            title: "AI 纠音朗读",
            config: {
              prompt: this.createForm.voice_prompt.trim(),
              min_seconds: 1,
              pass_score: Number(this.createForm.pass_score) || 60,
            },
          });
        }
        if (!tasks.length) {
          this.error = "请至少勾选并配置一种题型";
          return;
        }
        const body = {
          title: this.createForm.title.trim() || "综合练习作业",
          description: this.createForm.description,
          due_at: this.createForm.due_at || null,
          class_id: this.createForm.class_id ? Number(this.createForm.class_id) : null,
          assign_all_students: !this.createForm.class_id && this.createForm.assign_all_students,
          student_ids: !this.createForm.class_id && !this.createForm.assign_all_students
            ? this.createForm.student_ids.map(Number)
            : [],
          tasks,
          publish: this.createForm.publish,
        };
        try {
          const r = await fetch("/api/assignments", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify(body),
          }).then(this.ok);
          this.msg = "作业已创建 #" + r.id + "（学员 " + r.student_count + " 人）";
          this.createForm.title = "";
          this.tab = "assignments";
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      toggleId(listKey, id) {
        const list = this.createForm[listKey];
        const i = list.indexOf(id);
        if (i >= 0) list.splice(i, 1);
        else list.push(id);
      },
      async saveChoice(task) {
        const answers = {};
        for (const q of task.questions || []) answers[String(q.id)] = this.choiceAnswers[q.id] || this.choiceAnswers[String(q.id)] || "";
        try {
          const r = await fetch(`/api/assignments/${this.selected.id}/tasks/${task.id}/answer`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ answer: { answers } }),
          }).then(this.ok);
          this.msg = r.note || (r.score != null
            ? `选择题已提交：得分 ${r.score}`
            : "选择题已保存，交卷后公布对错");
          await this.openAssignment(this.selected);
        } catch (e) {
          this.error = e.message;
        }
      },
      async onMediaTime(e, task) {
        const t = Number(e.target.currentTime || 0);
        if (t > this.watchedOf(task.id)) this.videoWatchedByTask[task.id] = Math.floor(t);
        const last = this.mediaLastBeat[task.id] || 0;
        if (t - last < 4) return;
        const delta = Math.min(8, Math.max(0, t - last));
        this.mediaLastBeat[task.id] = t;
        await this.beatMedia(task, t, delta || 1);
      },
      async saveMediaTask(task) {
        try {
          const sess = await this.ensureMediaSession(task);
          const watched = this.watchedOf(task.id);
          await this.beatMedia(task, watched, 1);
          const r = await fetch(`/api/assignments/${this.selected.id}/tasks/${task.id}/answer`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({
              answer: {
                watched_seconds: this.watchedOf(task.id),
                completed: this.watchedOf(task.id) >= (task.config.min_seconds || 0),
                media_session_id: sess.session_id,
                media_token: sess.token,
              },
            }),
          }).then(this.ok);
          const label = task.task_type === "listen" ? "听力" : "视频";
          this.msg = r.answer.completed
            ? `${label}任务已完成`
            : `已有效观看 ${r.answer.watched_seconds}s，还需 ${r.answer.min_seconds}s`;
          await this.openAssignment(this.selected);
        } catch (e) {
          this.error = e.message;
        }
      },
      async saveVideo(task) {
        return this.saveMediaTask(task);
      },
      async saveDictation(task) {
        try {
          const text = this.dictationText[task.id] || "";
          const r = await fetch(`/api/assignments/${this.selected.id}/tasks/${task.id}/answer`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ answer: { text } }),
          }).then(this.ok);
          this.msg = r.note || (r.score != null
            ? `听写得分 ${r.score}`
            : "听写已保存，交卷后公布得分");
          await this.openAssignment(this.selected);
        } catch (e) {
          this.error = e.message;
        }
      },
      async updateAssignmentDue(item) {
        if (!this.editDueAt) {
          this.error = "请填写新的截止时间";
          return;
        }
        try {
          await fetch(`/api/assignments/${item.id}`, {
            method: "PUT",
            headers: authHeaders(),
            body: JSON.stringify({ due_at: this.editDueAt }),
          }).then(this.ok);
          this.msg = "截止时间已更新";
          this.editDueAt = "";
          await this.refresh();
          if (this.selected && this.selected.id === item.id) await this.openAssignment(item);
        } catch (e) {
          this.error = e.message;
        }
      },
      async reopenAssignment(item) {
        if (!this.editDueAt && !confirm("未填截止时间将清除截止日期并重开，确认？")) return;
        try {
          const body = this.editDueAt
            ? { due_at: this.editDueAt, reset_submitted: !!this.reopenResetSubmitted }
            : { clear_due_at: true, reset_submitted: !!this.reopenResetSubmitted };
          const r = await fetch(`/api/assignments/${item.id}/reopen`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify(body),
          }).then(this.ok);
          this.msg = r.reset_submitted
            ? `作业已重开，已打回 ${r.reset_submitted} 份交卷`
            : "作业已重开 #" + item.id;
          this.editDueAt = "";
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async archiveClass(item) {
        try {
          await fetch(`/api/classes/${item.id}`, {
            method: "PUT",
            headers: authHeaders(),
            body: JSON.stringify({ status: "archived" }),
          }).then(this.ok);
          this.msg = "班级已归档";
          this.classDetail = null;
          this.tab = "classes";
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async markWord(task, wordId) {
        // 只上报新增词，避免与服务端「每次最多 1 个新词」规则冲突时误传整表
        try {
          const r = await fetch(`/api/assignments/${this.selected.id}/tasks/${task.id}/answer`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ answer: { learned_word_ids: [Number(wordId)] } }),
          }).then(this.ok);
          this.msg = r.answer.completed ? "单词全部掌握" : `已掌握 ${r.answer.learned_word_ids.length}/${r.answer.total}`;
          await this.openAssignment(this.selected);
        } catch (e) {
          this.error = e.message;
        }
      },
      async startRecord(target, taskId = null) {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          this.chunks = [];
          this.recorder = new MediaRecorder(stream);
          this.recorder.ondataavailable = (e) => {
            if (e.data.size) this.chunks.push(e.data);
          };
          this.recorder.start();
          this.recording = true;
          this.recordTarget = target || "task";
          this.recordTaskId = taskId;
          this.recordSeconds = 0;
          this.startSpeechRecognition();
          this.recordTimer = setInterval(() => {
            this.recordSeconds += 1;
          }, 1000);
        } catch (e) {
          this.error = "无法访问麦克风：" + e.message;
        }
      },
      async stopRecord(task) {
        if (!this.recorder) return;
        if (this.recordTarget === "task" && task && this.recordTaskId && this.recordTaskId !== task.id) {
          this.error = "请在开始录音的同一任务上停止";
          return;
        }
        clearInterval(this.recordTimer);
        this.stopSpeechRecognition();
        const duration = this.recordSeconds;
        const transcript = this.liveTranscript || "";
        await new Promise((resolve) => {
          this.recorder.onstop = resolve;
          this.recorder.stop();
          this.recorder.stream.getTracks().forEach((t) => t.stop());
        });
        this.recording = false;
        const blob = new Blob(this.chunks, { type: "audio/webm" });
        const fd = new FormData();
        fd.append("file", blob, "voice.webm");
        fd.append("transcript", transcript);
        fd.append("duration_seconds", String(duration));
        try {
          if (this.recordTarget === "practice") {
            fd.append("reference_text", this.practiceText);
            const r = await fetch("/api/pronunciation/assess", {
              method: "POST",
              headers: authHeaders(false),
              body: fd,
            }).then(this.ok);
            this.pronResult = r.assessment;
            this.msg = `纠音完成：总分 ${r.assessment.overall_score}（${r.assessment.provider}）`;
            const history = await fetch("/api/pronunciation/history", { headers: authHeaders(false) }).then(this.ok);
            this.pronHistory = history.items || [];
          } else {
            const r = await fetch(
              `/api/assignments/${this.selected.id}/tasks/${task.id}/voice?duration_seconds=${duration}`,
              { method: "POST", headers: authHeaders(false), body: fd }
            ).then(this.ok);
            this.msg = `纠音得分 ${r.score}（${r.assessment.provider} / 准确度 ${r.assessment.accuracy_score || "-"}）`;
            await this.openAssignment(this.selected);
          }
        } catch (e) {
          this.error = e.message;
        }
      },
      async finalize() {
        try {
          const r = await fetch(`/api/assignments/${this.selected.id}/submit`, {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          this.msg = `作业已交卷，得分 ${r.score}`;
          await this.openAssignment(this.selected);
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async practiceKnow() {
        if (!this.currentWord) return;
        try {
          await fetch(`/api/my/vocab/${this.currentWord.id}/review`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ mastery: 100 }),
          }).then(this.ok);
          this.currentWord.mastery = 100;
          this.flipped = false;
          this.practiceIndex += 1;
          this.msg = "已标记掌握";
        } catch (e) {
          this.error = e.message;
        }
      },
      practiceNext() {
        this.flipped = false;
        this.practiceIndex += 1;
      },
      statusLabel(s) {
        return {
          draft: "草稿", published: "已发布", closed: "已关闭",
          in_progress: "进行中", submitted: "已提交", reviewed: "已批改",
          active: "进行中", archived: "已归档",
        }[s] || s || "未开始";
      },
      async publishAssignment(item) {
        try {
          const r = await fetch(`/api/assignments/${item.id}/publish`, {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          this.msg = "作业已发布 #" + item.id + (r.members_synced ? `（同步学员 ${r.members_synced}）` : "");
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async closeAssignment(item) {
        try {
          await fetch(`/api/assignments/${item.id}/close`, {
            method: "POST",
            headers: authHeaders(),
            body: "{}",
          }).then(this.ok);
          this.msg = "作业已关闭 #" + item.id;
          await this.refresh();
          if (this.selected && this.selected.id === item.id) await this.openAssignment(item);
        } catch (e) {
          this.error = e.message;
        }
      },
      async reviewSubmission(sub) {
        const form = this.reviewForms[sub.id];
        if (!form) {
          this.error = "批改表单未就绪，请重新打开作业";
          return;
        }
        try {
          await fetch(`/api/assignments/${this.selected.id}/submissions/${sub.id}/review`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({
              score: Number(form.score) || 0,
              teacher_comment: form.teacher_comment || null,
            }),
          }).then(this.ok);
          this.msg = `已批改 ${sub.display_name}`;
          await this.openAssignment(this.selected);
        } catch (e) {
          this.error = e.message;
        }
      },
      async loadCurriculum(silent = false) {
        if (!this.isTeacher) return;
        try {
          const d = await fetch("/api/textbooks", { headers: authHeaders(false) }).then(this.ok);
          this.textbooks = d.items || [];
          if (!this.chapterForm.textbook_id && this.textbooks.length) {
            this.chapterForm.textbook_id = this.textbooks[0].id;
          }
          if (!this.bindBookForm.textbook_id && this.textbooks.length) {
            this.bindBookForm.textbook_id = this.textbooks[0].id;
          }
          if (!this.bindBookForm.class_id && this.classes.length) {
            this.bindBookForm.class_id = this.classes[0].id;
          }
          if (!this.assignChapterForm.class_id && this.classes.length) {
            this.assignChapterForm.class_id = this.classes[0].id;
          }
        } catch (e) {
          if (!silent) this.error = e.message;
        }
      },
      async createTextbook() {
        if (!this.textbookForm.title.trim()) {
          this.error = "请填写教材名称";
          return;
        }
        try {
          await fetch("/api/textbooks", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify(this.textbookForm),
          }).then(this.ok);
          this.msg = "教材已创建";
          this.textbookForm = { title: "", level: "", description: "" };
          await this.loadCurriculum();
        } catch (e) {
          this.error = e.message;
        }
      },
      async openTextbook(book) {
        try {
          this.selectedTextbook = book;
          const d = await fetch(`/api/textbooks/${book.id}/chapters`, { headers: authHeaders(false) }).then(this.ok);
          this.chapters = d.items || [];
          this.chapterForm.textbook_id = book.id;
          if (this.chapters.length) this.assignChapterForm.chapter_id = this.chapters[0].id;
          this.tab = "curriculum";
        } catch (e) {
          this.error = e.message;
        }
      },
      async createChapter() {
        const tid = Number(this.chapterForm.textbook_id);
        if (!tid || !this.chapterForm.title.trim()) {
          this.error = "请选择教材并填写章节标题";
          return;
        }
        try {
          await fetch(`/api/textbooks/${tid}/chapters`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({
              title: this.chapterForm.title,
              objectives: this.chapterForm.objectives || null,
              word_ids: (this.chapterForm.word_ids || []).map(Number),
              question_ids: (this.chapterForm.question_ids || []).map(Number),
              resource_filenames: this.chapterForm.resource_filenames || [],
            }),
          }).then(this.ok);
          this.msg = "章节已创建";
          this.chapterForm.title = "";
          this.chapterForm.objectives = "";
          const book = this.textbooks.find((b) => b.id === tid) || this.selectedTextbook;
          if (book) await this.openTextbook(book);
          await this.loadCurriculum(true);
        } catch (e) {
          this.error = e.message;
        }
      },
      async bindTextbook() {
        const cid = Number(this.bindBookForm.class_id);
        const tid = Number(this.bindBookForm.textbook_id);
        if (!cid || !tid) {
          this.error = "请选择班级与教材";
          return;
        }
        try {
          await fetch(`/api/classes/${cid}/textbooks`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ textbook_id: tid }),
          }).then(this.ok);
          this.msg = "教材已绑定到班级";
        } catch (e) {
          this.error = e.message;
        }
      },
      async assignChapterHomework() {
        const ch = Number(this.assignChapterForm.chapter_id);
        if (!ch) {
          this.error = "请先打开教材并选择章节";
          return;
        }
        try {
          const r = await fetch(`/api/chapters/${ch}/assign-homework`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({
              class_id: this.assignChapterForm.class_id ? Number(this.assignChapterForm.class_id) : null,
              publish: !!this.assignChapterForm.class_id,
              include_dictation: !!this.assignChapterForm.include_dictation,
              include_voice: !!this.assignChapterForm.include_voice,
              due_days: Number(this.assignChapterForm.due_days) || 3,
            }),
          }).then(this.ok);
          this.msg = `已从章节生成作业 #${r.assignment_id}（${r.status}）`;
          await this.refresh();
        } catch (e) {
          this.error = e.message;
        }
      },
      async onHourStudentChange() {
        if (!this.hourForm.student_id) return;
        try {
          this.hourBalance = await fetch(`/api/hours/students/${this.hourForm.student_id}`, {
            headers: authHeaders(false),
          }).then(this.ok);
        } catch (e) {
          this.error = e.message;
        }
      },
      async onMediaPlay(task) {
        try {
          await this.ensureMediaSession(task);
        } catch (_) {
          /* error already set */
        }
      },
      async createWordQuick() {
        const word = (this.quickWord && this.quickWord.word || "").trim();
        const meaning = (this.quickWord && this.quickWord.meaning || "").trim();
        if (!word || !meaning) { this.error = "请填写单词与释义"; return; }
        try {
          const r = await fetch("/api/words", {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({
              word,
              meaning,
              phonetic: (this.quickWord.phonetic || "").trim() || null,
              example_sentence: (this.quickWord.example || "").trim() || null,
            }),
          }).then(this.ok);
          this.words.push(r);
          this.createForm.word_ids.push(r.id);
          this.quickWord = { word: "", meaning: "", phonetic: "", example: "" };
          this.msg = "单词已添加 #" + r.id;
        } catch (e) { this.error = e.message; }
      },
      async createQuestionQuick() {
        const q = this.quickQuestion || {};
        if (!(q.stem || "").trim() || !(q.option_a || "").trim()) {
          this.error = "请填写题干与选项";
          return;
        }
        try {
          const r = await fetch("/api/questions", {
            method: "POST", headers: authHeaders(),
            body: JSON.stringify({
              stem: q.stem.trim(),
              option_a: q.option_a.trim(),
              option_b: (q.option_b || "").trim() || "B",
              option_c: (q.option_c || "").trim() || "C",
              option_d: (q.option_d || "").trim() || "D",
              answer: (q.answer || "A").toUpperCase(),
              explanation: (q.explanation || "").trim() || null,
            }),
          }).then(this.ok);
          this.questions.push(r);
          this.createForm.question_ids.push(r.id);
          this.quickQuestion = { stem: "", option_a: "", option_b: "", option_c: "", option_d: "", answer: "A", explanation: "" };
          this.msg = "题目已添加 #" + r.id;
        } catch (e) { this.error = e.message; }
      },
    },
  }).mount("#learn-app");
})();

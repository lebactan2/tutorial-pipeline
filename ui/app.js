document.addEventListener('DOMContentLoaded', () => {
    const videoList = document.getElementById('videoList');
    const videoSourceInput = document.getElementById('videoSource');
    const downloadBtn = document.getElementById('downloadBtn');
    const statusMessage = document.getElementById('statusMessage');
    
    const reviewArea = document.getElementById('reviewArea');
    const emptyState = document.getElementById('emptyState');
    const currentVideoTitle = document.getElementById('currentVideoTitle');
    
    const player = document.getElementById('player');
    const subtitlesTrack = document.getElementById('subtitlesTrack');
    const srtContent = document.getElementById('srtContent');
    
    const videoControls = document.getElementById('videoControls');
    const btnLocal = document.getElementById('btnLocal');
    const btnCloud = document.getElementById('btnCloud');

    // New Elements
    const btnSettings = document.getElementById('btnSettings');
    const settingsModal = document.getElementById('settingsModal');
    const btnCloseSettings = document.getElementById('btnCloseSettings');
    const btnSaveSettings = document.getElementById('btnSaveSettings');
    const settingsStatus = document.getElementById('settingsStatus');
    const llmProvider = document.getElementById('llmProvider');
    const openrouterGroup = document.getElementById('openrouterGroup');
    const openrouterModel = document.getElementById('openrouterModel');
    const openrouterCustomModel = document.getElementById('openrouterCustomModel');
    const customApiGroup = document.getElementById('customApiGroup');
    const customApiKeyGroup = document.getElementById('customApiKeyGroup');
    
    const voiceSelect = document.getElementById('voiceSelect');
    const btnUploadVoice = document.getElementById('btnUploadVoice');
    const voiceUploadInput = document.getElementById('voiceUploadInput');
    const transcriptUpload = document.getElementById('transcriptUpload');

    let currentVideoData = null;
    let uploadedTranscriptPath = null;

    // Load available videos
    async function loadVideos() {
        try {
            const res = await fetch('/api/videos');
            const videos = await res.json();
            renderVideoList(videos);
        } catch (e) {
            console.error("Failed to load videos", e);
        }
    }

    function renderVideoList(videos) {
        videoList.innerHTML = '';
        if (videos.length === 0) {
            videoList.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">No videos generated yet.</p>';
            return;
        }

        videos.forEach(v => {
            const li = document.createElement('li');
            li.className = 'video-item';
            
            let tagsHtml = '';
            if (v.local_draft) tagsHtml += '<span class="tag available">Local</span>';
            if (v.cloud_draft) tagsHtml += '<span class="tag available">Cloud</span>';
            if (!v.local_draft && !v.cloud_draft) tagsHtml += '<span class="tag">Processing...</span>';

            li.innerHTML = `
                <h4>${v.id}</h4>
                <div class="tags">${tagsHtml}</div>
            `;
            
            if (currentVideoData && currentVideoData.id === v.id) {
                li.classList.add('active');
                // Update current reference silently if something changed
                currentVideoData = v; 
            }

            li.addEventListener('click', () => selectVideo(v, li));
            videoList.appendChild(li);
        });
    }

    function selectVideo(videoData, element) {
        document.querySelectorAll('.video-item').forEach(el => el.classList.remove('active'));
        if (element) element.classList.add('active');

        currentVideoData = videoData;
        emptyState.classList.add('hidden');
        reviewArea.classList.remove('hidden');
        videoControls.classList.remove('hidden');
        
        currentVideoTitle.textContent = videoData.id;

        // Reset toggles
        btnLocal.classList.remove('active');
        btnCloud.classList.remove('active');

        if (videoData.local_draft) {
            btnLocal.style.display = 'block';
            loadPlayer(videoData, 'local_draft');
            btnLocal.classList.add('active');
        } else {
            btnLocal.style.display = 'none';
        }

        if (videoData.cloud_draft) {
            btnCloud.style.display = 'block';
            if (!videoData.local_draft) {
                loadPlayer(videoData, 'cloud_draft');
                btnCloud.classList.add('active');
            }
        } else {
            btnCloud.style.display = 'none';
        }

        loadSrt(videoData.srt);
    }

    function loadPlayer(videoData, type) {
        const time = player.currentTime;
        player.src = `/files/output/${videoData[type]}`;
        if (videoData.srt) {
            subtitlesTrack.src = `/files/output/${videoData.srt}`;
        }
        player.load();
        
        player.addEventListener('loadedmetadata', function onLoaded() {
            if (time > 0) {
                player.currentTime = time;
            }
            player.removeEventListener('loadedmetadata', onLoaded);
        });
    }

    async function loadSrt(srtFile) {
        if (!srtFile) {
            srtContent.textContent = "No subtitles available.";
            return;
        }
        try {
            const res = await fetch(`/files/output/${srtFile}`);
            if (res.ok) {
                const text = await res.text();
                srtContent.textContent = text;
            } else {
                srtContent.textContent = "Failed to load subtitles.";
            }
        } catch (e) {
            srtContent.textContent = "Error loading subtitles.";
        }
    }

    // Toggle handlers
    btnLocal.addEventListener('click', () => {
        if (!currentVideoData || !currentVideoData.local_draft) return;
        btnLocal.classList.add('active');
        btnCloud.classList.remove('active');
        loadPlayer(currentVideoData, 'local_draft');
        player.play();
    });

    btnCloud.addEventListener('click', () => {
        if (!currentVideoData || !currentVideoData.cloud_draft) return;
        btnCloud.classList.add('active');
        btnLocal.classList.remove('active');
        loadPlayer(currentVideoData, 'cloud_draft');
        player.play();
    });

    // Voice Upload
    btnUploadVoice.addEventListener('click', () => {
        voiceUploadInput.click();
    });

    voiceUploadInput.addEventListener('change', async () => {
        const file = voiceUploadInput.files[0];
        if (!file) return;
        
        const formData = new FormData();
        formData.append('file', file);
        
        try {
            const res = await fetch('/api/voices', { method: 'POST', body: formData });
            const data = await res.json();
            if (res.ok) {
                loadVoices();
            } else {
                alert(data.error || "Failed to upload voice");
            }
        } catch(e) {
            alert("Error uploading voice");
        }
    });

    // Transcript Upload
    transcriptUpload.addEventListener('change', async () => {
        const file = transcriptUpload.files[0];
        if (!file) {
            uploadedTranscriptPath = null;
            return;
        }
        
        const formData = new FormData();
        formData.append('file', file);
        
        try {
            const res = await fetch('/api/upload_transcript', { method: 'POST', body: formData });
            const data = await res.json();
            if (res.ok) {
                uploadedTranscriptPath = data.transcript_path;
            } else {
                alert(data.error || "Failed to upload transcript");
                transcriptUpload.value = "";
                uploadedTranscriptPath = null;
            }
        } catch(e) {
            alert("Error uploading transcript");
            transcriptUpload.value = "";
            uploadedTranscriptPath = null;
        }
    });

    // Download handler
    downloadBtn.addEventListener('click', async () => {
        const videoSource = videoSourceInput.value.trim();
        if (!videoSource) return;

        videoSourceInput.disabled = true;
        downloadBtn.disabled = true;
        statusMessage.classList.remove('hidden', 'error');
        
        let skipTranscribe = false;
        let forceTranscribe = false;

        // Check if transcript exists (only if not providing a custom one)
        if (!uploadedTranscriptPath) {
            statusMessage.textContent = "Checking video...";
            try {
                const checkRes = await fetch('/api/check_video', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ video_source: videoSource })
                });
                if (checkRes.ok) {
                    const checkData = await checkRes.json();
                    if (checkData.transcript_exists) {
                        const skip = confirm("A transcript already exists for this video. Do you want to Skip Transcription (use existing)?\n\nClick OK to Skip, Cancel to Overwrite.");
                        if (skip) {
                            skipTranscribe = true;
                        } else {
                            forceTranscribe = true;
                        }
                    }
                }
            } catch (e) {
                console.error("Failed to check video", e);
            }
        }

        statusMessage.textContent = "Starting pipeline...";

        try {
            const payload = { 
                video_source: videoSource, 
                transcript_path: uploadedTranscriptPath,
                voice_ref: voiceSelect.value,
                skip_transcribe: skipTranscribe,
                force_transcribe: forceTranscribe
            };
            const res = await fetch('/api/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            
            if (res.ok) {
                statusMessage.textContent = "Pipeline running in background! Check terminal for logs.";
                videoSourceInput.value = '';
                transcriptUpload.value = '';
                uploadedTranscriptPath = null;
                setTimeout(loadVideos, 5000);
            } else {
                statusMessage.textContent = data.error || "Failed to start pipeline.";
                statusMessage.classList.add('error');
            }
        } catch (e) {
            statusMessage.textContent = "Network error occurred.";
            statusMessage.classList.add('error');
        } finally {
            videoSourceInput.disabled = false;
            downloadBtn.disabled = false;
            setTimeout(() => {
                if(!statusMessage.classList.contains('error')) {
                    statusMessage.classList.add('hidden');
                }
            }, 8000);
        }
    });

    // Settings Modal Logic
    function updateSettingsVisibility() {
        const provider = llmProvider.value;
        openrouterGroup.style.display = provider === 'openrouter' ? 'block' : 'none';
        customApiGroup.style.display = provider === 'custom' ? 'block' : 'none';
        customApiKeyGroup.style.display = provider === 'custom' ? 'block' : 'none';
        
        if (provider === 'openrouter') {
            openrouterCustomModel.style.display = openrouterModel.value === 'custom' ? 'block' : 'none';
        }
    }

    llmProvider.addEventListener('change', updateSettingsVisibility);
    openrouterModel.addEventListener('change', updateSettingsVisibility);

    btnSettings.addEventListener('click', async () => {
        try {
            const res = await fetch('/api/settings');
            const data = await res.json();
            
            document.getElementById('keyAnthropic').value = data.ANTHROPIC_API_KEY || '';
            document.getElementById('keyAntigravity').value = data.ANTIGRAVITY_API_KEY || '';
            document.getElementById('keyOpenRouter').value = data.OPENROUTER_API_KEY || '';
            document.getElementById('keyMinimax').value = data.MINIMAX_API_KEY || '';
            document.getElementById('keyCustom').value = data.CUSTOM_API_KEY || '';
            document.getElementById('customApiBaseUrl').value = data.CUSTOM_API_BASE_URL || '';
            
            llmProvider.value = data.llm_provider || 'claude';
            
            if (data.openrouter_model) {
                if (Array.from(openrouterModel.options).some(o => o.value === data.openrouter_model)) {
                    openrouterModel.value = data.openrouter_model;
                } else {
                    openrouterModel.value = 'custom';
                    openrouterCustomModel.value = data.openrouter_model;
                }
            }
            document.getElementById('customModel').value = data.custom_model || '';
            
            updateSettingsVisibility();
            settingsModal.classList.remove('hidden');
        } catch(e) {
            console.error(e);
        }
    });

    btnCloseSettings.addEventListener('click', () => {
        settingsModal.classList.add('hidden');
    });

    btnSaveSettings.addEventListener('click', async () => {
        const payload = {
            ANTHROPIC_API_KEY: document.getElementById('keyAnthropic').value,
            ANTIGRAVITY_API_KEY: document.getElementById('keyAntigravity').value,
            MINIMAX_API_KEY: document.getElementById('keyMinimax').value,
            OPENROUTER_API_KEY: document.getElementById('keyOpenRouter').value,
            CUSTOM_API_BASE_URL: document.getElementById('customApiBaseUrl').value,
            CUSTOM_API_KEY: document.getElementById('keyCustom').value,
            llm_provider: llmProvider.value,
            openrouter_model: openrouterModel.value === 'custom' ? openrouterCustomModel.value : openrouterModel.value,
            custom_model: document.getElementById('customModel').value
        };

        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                settingsStatus.classList.remove('hidden');
                setTimeout(() => settingsStatus.classList.add('hidden'), 3000);
            }
        } catch(e) {
            console.error(e);
        }
    });

    // Load voices
    async function loadVoices() {
        try {
            const res = await fetch('/api/voices');
            const voices = await res.json();
            
            const selected = voiceSelect.value;
            voiceSelect.innerHTML = '<option value="">Default Voice</option>';
            
            voices.forEach(v => {
                const opt = document.createElement('option');
                opt.value = v;
                opt.textContent = v;
                voiceSelect.appendChild(opt);
            });
            
            if (voices.includes(selected)) {
                voiceSelect.value = selected;
            }
        } catch(e) {
            console.error(e);
        }
    }

    // Initial load
    loadVideos();
    loadVoices();
    setInterval(loadVideos, 10000); // Poll for updates
});

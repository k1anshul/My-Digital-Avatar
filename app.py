import os
import asyncio
import streamlit as st
import threading
import time
import random

import pyaudio
from google import genai
from google.genai import types
from dotenv import load_dotenv
from utils import *

# Load environment variables
load_dotenv()

# Audio configuration - Optimized for stable streaming
FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 512  # Smaller chunks for better real-time performance
BUFFER_SIZE = 8192  # Larger buffer to prevent dropouts

MODEL = "models/gemini-2.5-flash-preview-native-audio-dialog"

# Initialize Streamlit page
st.set_page_config(
    page_title="My Digital Avatar",
    page_icon="🎤",
    layout="wide"
)

# Custom CSS for avatar animations
st.markdown(avatar_backgroud_css, unsafe_allow_html=True)

st.title("🎤 Meet Anshul")
st.markdown("**Data Scientist | AWS ML Specialist | AI Expert** - Ask me about my experience, skills, and career!")
st.markdown("*Real-time voice conversation - I'll tell you all about my background in AI, ML, and Data Science*")

# Initialize session state
if 'chat_active' not in st.session_state:
    st.session_state.chat_active = False
if 'conversation_log' not in st.session_state:
    st.session_state.conversation_log = []
if 'audio_loop' not in st.session_state:
    st.session_state.audio_loop = None
if 'avatar_state' not in st.session_state:
    st.session_state.avatar_state = 'idle'  # idle, listening, speaking
if 'is_speaking' not in st.session_state:
    st.session_state.is_speaking = False
if 'last_activity' not in st.session_state:
    st.session_state.last_activity = time.time()

class StreamlitAudioChat:
    def __init__(self):
        # Initialize Google Gemini client
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            st.error("Please set your GOOGLE_API_KEY in the .env file")
            st.stop()
        
        self.client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=api_key,
        )
        
        # Configuration for audio-only mode with system instructions
        self.config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            media_resolution="MEDIA_RESOLUTION_MEDIUM",
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Fenrir")
                )
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=types.SlidingWindow(target_tokens=12800),
            ),
            system_instruction=about_my_self
        )
        
        # Initialize PyAudio
        self.pya = pyaudio.PyAudio()
        self.audio_stream = None
        self.output_stream = None
        
        # Queues for audio data
        self.audio_in_queue = None
        self.out_queue = None
        
        # Session and tasks
        self.session = None
        self.tasks = []
        self.loop = None
        self.running = False

    async def listen_audio(self):
        """Capture audio from microphone with improved buffering"""
        try:
            mic_info = self.pya.get_default_input_device_info()
            self.audio_stream = await asyncio.to_thread(
                self.pya.open,
                format=FORMAT,
                channels=CHANNELS,
                rate=SEND_SAMPLE_RATE,
                input=True,
                input_device_index=mic_info["index"],
                frames_per_buffer=CHUNK_SIZE,
                stream_callback=None,  # Use blocking mode for better control
            )
            
            while self.running:
                try:
                    # Use smaller chunks for better responsiveness
                    data = await asyncio.to_thread(
                        self.audio_stream.read, 
                        CHUNK_SIZE, 
                        exception_on_overflow=False
                    )
                    
                    # Simple voice activity detection with better threshold
                    import audioop
                    volume = audioop.rms(data, 2)
                    if volume > 300:  # Lower threshold for better sensitivity
                        st.session_state.avatar_state = 'listening'
                        st.session_state.last_activity = time.time()
                    
                    # Only send audio if not currently playing response
                    if not self.is_playing_audio:
                        try:
                            await asyncio.wait_for(
                                self.out_queue.put({"data": data, "mime_type": "audio/pcm"}),
                                timeout=0.1  # Prevent blocking
                            )
                        except asyncio.TimeoutError:
                            pass  # Skip this chunk if queue is full
                    
                    # Small delay to prevent overwhelming the system
                    await asyncio.sleep(0.01)
                    
                except Exception as e:
                    if self.running:
                        print(f"Audio input error: {e}")  # Log instead of showing to user
                    break
        except Exception as e:
            st.error(f"Failed to initialize audio input: {e}")

    async def send_realtime(self):
        """Send audio data to Gemini with better error handling"""
        while self.running:
            try:
                # Use timeout to prevent hanging
                msg = await asyncio.wait_for(self.out_queue.get(), timeout=1.0)
                if self.session and not self.is_playing_audio:
                    await self.session.send(input=msg)
            except asyncio.TimeoutError:
                continue  # Continue loop if no message received
            except Exception as e:
                if self.running:
                    print(f"Send error: {e}")
                break

    async def receive_audio(self):
        """Receive audio responses from Gemini with improved handling"""
        while self.running:
            try:
                turn = self.session.receive()
                self.is_playing_audio = False
                
                async for response in turn:
                    if not self.running:
                        break
                        
                    if data := response.data:
                        if not self.is_playing_audio:
                            st.session_state.avatar_state = 'speaking'
                            self.is_playing_audio = True
                        
                        # Use timeout to prevent queue blocking
                        try:
                            await asyncio.wait_for(
                                self.audio_in_queue.put(data),
                                timeout=0.5
                            )
                        except asyncio.TimeoutError:
                            print("Audio queue full, skipping chunk")
                        continue
                        
                    if text := response.text:
                        # Add text to conversation log
                        st.session_state.conversation_log.append({
                            'type': 'ai',
                            'message': text,
                            'timestamp': time.time()
                        })

                # Reset state after turn completion
                self.is_playing_audio = False
                        
            except Exception as e:
                if self.running:
                    print(f"Receive error: {e}")
                break

    async def play_audio(self):
        """Play audio responses with improved buffering"""
        try:
            self.output_stream = await asyncio.to_thread(
                self.pya.open,
                format=FORMAT,
                channels=CHANNELS,
                rate=RECEIVE_SAMPLE_RATE,
                output=True,
                frames_per_buffer=BUFFER_SIZE,  # Larger buffer for output
            )
            
            while self.running:
                try:
                    # Use timeout to prevent hanging
                    bytestream = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=1.0
                    )
                    
                    if self.output_stream and bytestream:
                        await asyncio.to_thread(self.output_stream.write, bytestream)
                        
                except asyncio.TimeoutError:
                    continue  # Continue loop if no audio received
                except Exception as e:
                    if self.running:
                        print(f"Audio playback error: {e}")
                    break
                    
        except Exception as e:
            st.error(f"Failed to initialize audio output: {e}")

    async def run_chat(self):
        """Main chat loop with improved error handling and stability"""
        try:
            self.running = True
            async with self.client.aio.live.connect(model=MODEL, config=self.config) as session:
                self.session = session
                
                # Initialize queues with appropriate sizes
                self.audio_in_queue = asyncio.Queue(maxsize=50)  # Larger buffer for audio output
                self.out_queue = asyncio.Queue(maxsize=10)       # Moderate buffer for input
                
                # Create tasks with better error isolation
                async with asyncio.TaskGroup() as tg:
                    # Start all tasks
                    send_task = tg.create_task(self.send_realtime())
                    listen_task = tg.create_task(self.listen_audio())
                    receive_task = tg.create_task(self.receive_audio())
                    play_task = tg.create_task(self.play_audio())
                    
                    # Keep running until stopped
                    while self.running:
                        await asyncio.sleep(0.05)  # Shorter sleep for better responsiveness
                        
                        # Check if any critical task has failed
                        if (send_task.done() or listen_task.done() or 
                            receive_task.done() or play_task.done()):
                            # If any task completed unexpectedly, log and continue
                            for task in [send_task, listen_task, receive_task, play_task]:
                                if task.done() and not task.cancelled():
                                    try:
                                        task.result()
                                    except Exception as e:
                                        print(f"Task error: {e}")
                        
        except Exception as e:
            print(f"Chat error: {e}")
            st.error("Connection lost. Please restart the chat.")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources"""
        self.running = False
        if self.audio_stream:
            self.audio_stream.close()
        if self.output_stream:
            self.output_stream.close()

def get_avatar_face(state):
    """Get avatar face based on current state"""
    faces = {
        'idle': ["""<img src='https://avataaars.io/?avatarStyle=Circle&topType=ShortHairShortCurly&accessoriesType=Prescription02&hairColor=Black&facialHairType=BeardLight&facialHairColor=Black&clotheType=CollarSweater&clotheColor=Black&eyeType=Default&eyebrowType=Default&mouthType=Twinkle&skinColor=Light'
/>""","""<img src='https://avataaars.io/?avatarStyle=Circle&topType=ShortHairShortCurly&accessoriesType=Prescription02&hairColor=Black&facialHairType=BeardLight&facialHairColor=Black&clotheType=CollarSweater&clotheColor=Black&eyeType=Default&eyebrowType=Default&mouthType=Smile&skinColor=Light'
/>"""],
    'listening': ['👂', '🎧', '👁️'],
    'speaking': ['💬', '🗣️', '😊', '💡']
    }
    return random.choice(faces[state])

def get_status_text(state):
    """Get status text based on avatar state"""
    status_texts = {
        'idle': 'Ask me about my experience!',
        'listening': 'Listening to your question...',
        'speaking': 'Sharing my experience...'
    }
    return status_texts[state]

def run_chat_in_thread(chat_instance):
    """Run the chat in a separate thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(chat_instance.run_chat())
    except Exception as e:
        st.error(f"Thread error: {e}")
    finally:
        loop.close()

# Update avatar state based on activity
if st.session_state.chat_active:
    current_time = time.time()
    if st.session_state.is_speaking:
        st.session_state.avatar_state = 'speaking'
    elif current_time - st.session_state.last_activity < 2:
        if st.session_state.avatar_state != 'speaking':
            st.session_state.avatar_state = 'listening'
    else:
        st.session_state.avatar_state = 'idle'

# Main layout
col1, col2 = st.columns([1, 1])

with col1:
    # Avatar section
    avatar_face = get_avatar_face(st.session_state.avatar_state)
    status_text = get_status_text(st.session_state.avatar_state)
    
    st.markdown(f"""
    <div class="avatar-container">
        <div class="avatar {st.session_state.avatar_state}">
            <div class="avatar-face">{avatar_face}</div>
        </div>
    </div>
    <div class="status-text">{status_text}</div>
    """, unsafe_allow_html=True)
    
    # Audio visualizer when active
    if st.session_state.chat_active and st.session_state.avatar_state in ['listening', 'speaking']:
        st.markdown("""
        <div class="audio-visualizer">
            <div class="bar"></div>
            <div class="bar"></div>
            <div class="bar"></div>
            <div class="bar"></div>
            <div class="bar"></div>
        </div>
        """, unsafe_allow_html=True)
    
    # Chat controls
    if not st.session_state.chat_active:
        if st.button("🎤 Start Conversation", type="primary", use_container_width=True):
            st.session_state.chat_active = True
            st.session_state.conversation_log = []
            st.session_state.avatar_state = 'speaking'
            
            # Create and start chat instance
            chat = StreamlitAudioChat()
            st.session_state.audio_loop = chat
            
            # Start chat in background thread
            chat_thread = threading.Thread(target=run_chat_in_thread, args=(chat,))
            chat_thread.daemon = True
            chat_thread.start()
            
            st.success("🎤 Ready for questions! Ask me about my experience, skills, or background...")
            st.rerun()
    else:
        if st.button("⏹️ End Interview/Chat", type="secondary", use_container_width=True):
            st.session_state.chat_active = False
            st.session_state.avatar_state = 'idle'
            if st.session_state.audio_loop:
                st.session_state.audio_loop.cleanup()
                st.session_state.audio_loop = None
            st.success("Interview/Chat ended!")
            st.rerun()

with col2:
    # Status and conversation
    if st.session_state.chat_active:
        st.success("🔴 LIVE")
        st.info("💡 Ask me about my skills, experience, projects, or background!")
        
        # Quick question suggestions
        st.markdown("**Quick questions you can ask:**")
        st.markdown("• *What's your current role?*")
        st.markdown("• *Tell me about your AI/ML experience*")
        st.markdown("• *What are your key achievements?*")
        st.markdown("• *What technologies do you work with?*")
        st.markdown("• *Tell me about your education*")
    else:
        st.info("⚪ Ready for chat")
        
        # Profile highlights when inactive
        st.markdown("**👨‍💻 Profile Highlights:**")
        st.markdown("• **Current Role**: Data Scientist at Fidelity Investments")
        st.markdown("• **Expertise**: AI, ML, NLP, Generative AI")
        st.markdown("• **Experience**: 5+ years in Data Science")
        st.markdown("• **Education**: Master in ML & AI from Liverpool John Moores University")
        st.markdown("• **Certifications**: AWS ML, Deep Learning, Generative AI")

# Contact Information Footer
st.markdown("---")
col1, col2, col3 = st.columns(3)
with col1:
    st.markdown("📧 **Email**: anshulkhadse2011@gmail.com")
with col2:
    st.markdown("🔗 **LinkedIn**: [k1anshul](https://www.linkedin.com/in/k1anshul)")
with col3:
    st.markdown("💻 **GitHub**: [k1anshul](https://github.com/k1anshul)")

# Auto-refresh when chat is active for avatar animations
if st.session_state.chat_active:
    time.sleep(0.5)
    st.rerun() 

"""Integration test for gRPC voice system"""
import asyncio
import grpc
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_pb2
import voice_pb2_grpc

async def test_session():
    """Test a complete gRPC session"""
    try:
        channel = grpc.aio.insecure_channel("localhost:60015")
        stub = voice_pb2_grpc.VoiceServiceStub(channel)
        
        # Create a test audio chunk (silent audio)
        test_audio = b'\x00' * 1024  # 1024 bytes of silence
        
        async def request_generator():
            # Send audio chunk
            yield voice_pb2.ClientMessage(audio_chunk=test_audio)
            await asyncio.sleep(0.1)
            
            # Send interrupt
            yield voice_pb2.ClientMessage(interrupt=True)
            await asyncio.sleep(0.1)
            
            # Send more audio
            yield voice_pb2.ClientMessage(audio_chunk=test_audio)
            
        print("Starting gRPC session...")
        response_count = 0
        
        async for response in stub.Session(request_generator()):
            response_count += 1
            if response.HasField('audio_response'):
                print(f"Received audio chunk: {len(response.audio_response)} bytes")
            elif response.HasField('text_status'):
                print(f"Status: {response.text_status}")
                
            if response_count >= 10:
                break
                
        print(f"Session completed. Received {response_count} responses.")
        await channel.close()
        return True
        
    except Exception as e:
        print(f"Session failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(test_session())
    sys.exit(0 if success else 1)

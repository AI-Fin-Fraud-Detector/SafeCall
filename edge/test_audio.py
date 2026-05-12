"""Test with simulated audio input"""
import asyncio
import grpc
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_pb2
import voice_pb2_grpc

SAMPLE_RATE = 16000

async def test_audio_session():
    """Send simulated audio and check response"""
    try:
        channel = grpc.aio.insecure_channel("localhost:60015")
        stub = voice_pb2_grpc.VoiceServiceStub(channel)
        
        # Generate 1 second of silence (int16)
        samples = np.zeros(SAMPLE_RATE, dtype=np.int16)
        chunk_size = 512
        audio_chunks = []
        
        for i in range(0, len(samples), chunk_size):
            chunk = samples[i:i+chunk_size].tobytes()
            audio_chunks.append(chunk)
            
        print(f"Sending {len(audio_chunks)} audio chunks...")
        
        async def request_generator():
            for chunk in audio_chunks:
                yield voice_pb2.ClientMessage(audio_chunk=chunk)
                await asyncio.sleep(0.01)  # Simulate real-time
                
        response_count = 0
        
        async for response in stub.Session(request_generator()):
            response_count += 1
            if response.HasField('audio_response'):
                print(f"✓ Received audio: {len(response.audio_response)} bytes")
            elif response.HasField('text_status'):
                print(f"✓ Status: {response.text_status}")
                
            if response_count >= 5:
                break
                
        print(f"\n✓ Test completed. Received {response_count} responses.")
        await channel.close()
        return True
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(test_audio_session())
    sys.exit(0 if success else 1)

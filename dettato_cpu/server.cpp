// dettato_server: one ensemble job on the CPU with dettato.cpp, for app/transcription/ensemble/cpu.py.
//
//   dettato_server MODEL.gguf AUDIO.f32 RATE SOURCE_RATE THREADS OUT.json
//
// AUDIO.f32: mono float32 samples (little endian) at RATE. SOURCE_RATE: the rate the recording's source had; the drum
// model runs only for 40 kHz or more (the server converts every recording to 44.1 kHz, so RATE does not tell).
// THREADS: CPU threads, 0 for dettato.cpp's default. Prints "progress <0..1>" lines on stdout while it runs, writes
// OUT.json (through OUT.json.partial, then renamed), and exits 0; on an error it prints "error: ..." on stderr and exits 1.
// The caller cancels a job by ending the process.

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "dettato/dettato.hpp"

namespace fs = std::filesystem;

static std::string jsonString(const std::string& s)
{
    std::string o = "\"";
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') {
            o += '\\';
            o += static_cast<char>(c);
        } else if (c < 0x20) {
            char buf[8];
            std::snprintf(buf, sizeof buf, "\\u%04x", c);
            o += buf;
        } else {
            o += static_cast<char>(c);
        }
    }
    return o + "\"";
}

static int fail(const std::string& message)
{
    std::fprintf(stderr, "error: %s\n", message.c_str());
    return 1;
}

int main(int argc, char** argv)
{
    if (argc != 7) {
        return fail("usage: dettato_server MODEL.gguf AUDIO.f32 RATE SOURCE_RATE THREADS OUT.json");
    }
    const std::string model = argv[1], audio_path = argv[2], out_path = argv[6];
    const double rate = std::atof(argv[3]);
    const double source_rate = std::atof(argv[4]);
    const int threads = std::atoi(argv[5]);
    if (!(rate >= 16000) || !(source_rate > 0) || threads < 0) {
        return fail("invalid RATE, SOURCE_RATE or THREADS");
    }

    std::ifstream in(audio_path, std::ios::binary | std::ios::ate);
    if (!in) {
        return fail("cannot open the audio");
    }
    const auto bytes = static_cast<size_t>(in.tellg());
    if (bytes % sizeof(float)) {
        return fail("the audio is not float32 samples");
    }
    std::vector<float> samples(bytes / sizeof(float));
    in.seekg(0);
    if (!in.read(reinterpret_cast<char*>(samples.data()), static_cast<std::streamsize>(bytes))) {
        return fail("cannot read the audio");
    }
    in.close();

    dtt::LoadOptions lo;
    lo.n_threads = threads;
    lo.device = dtt::availableDevices().size() - 1;      // the CPU, always last
    std::string detail;
    lo.error_detail = &detail;
    auto pipe = dtt::Pipeline::load(model, lo);
    if (!pipe) {
        return fail(std::string("cannot load the model: ") + dtt::describe(pipe.error()) + " " + detail);
    }

    dtt::TranscribeOptions to;
    to.drums = source_rate >= 40000;
    auto last = std::chrono::steady_clock::now() - std::chrono::seconds(1);
    to.on_progress = [&](const dtt::Progress& p) {
        const auto now = std::chrono::steady_clock::now();
        if (now - last >= std::chrono::milliseconds(500)) {
            last = now;
            std::printf("progress %.4f\n", p.overall);
            std::fflush(stdout);
        }
    };
    auto r = pipe->transcribe(samples, rate, to);
    if (!r) {
        return fail(std::string(dtt::describe(r.error())) + " " + pipe->lastErrorMessage());
    }

    const std::string partial = out_path + ".partial";
    {
        std::ofstream js(partial, std::ios::trunc);
        if (!js) {
            return fail("cannot write the result");
        }
        js.precision(9);
        js << "{\"seconds\": " << r->seconds << ", \"tuning_cents\": " << r->tuning_cents
           << ", \"drum_model\": " << jsonString(r->drum_model) << ", \"transcribed\": " << r->notes_transcribed
           << ", \"dropped\": " << r->notes_dropped << ", \"inserted\": " << r->notes_inserted << ", \"tracks\": [";
        for (size_t k = 0; k < r->tracks.size(); k++) {
            const auto& t = r->tracks[k];
            js << (k ? ", " : "") << "{\"name\": " << jsonString(t.name) << ", \"program\": " << t.program
               << ", \"is_drum\": " << (t.is_drum ? "true" : "false") << "}";
        }
        js << "], \"notes\": [";
        for (size_t i = 0; i < r->notes.size(); i++) {
            const auto& n = r->notes[i];
            js << (i ? ", " : "") << "[" << n.onset << ", " << n.offset << ", " << n.pitch << ", " << n.velocity << ", "
               << n.track << "]";
        }
        js << "]}\n";
        js.flush();
        if (!js) {
            return fail("cannot write the result");
        }
    }
    std::error_code ec;
    fs::rename(partial, out_path, ec);
    if (ec) {
        return fail("cannot write the result: " + ec.message());
    }
    std::printf("progress 1\n");
    return 0;
}


using System.Numerics;

using MathNet.Numerics.IntegralTransforms;

using Newtonsoft.Json;

namespace MarvinsAIRARefactored.Components;

// ── Filter type ────────────────────────────────────────────────────────────

public enum FilterType
{
	Peak,
	LowShelf,
	HighShelf
}

// ── Single EQ band definition (JSON-serializable) ─────────────────────────

public class EqBand
{
	public float      Freq    { get; set; } = 10f;
	public float      GainDb  { get; set; } = 0f;
	public float      Q       { get; set; } = 1.4f;
	public FilterType Type    { get; set; } = FilterType.Peak;
	public bool       Enabled { get; set; } = true;

	// Returns SOS row: [b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]
	// Formulas ported from ffb_analyzer_v2.py EQBand.get_sos()
	public double[] GetSos( float fs )
	{
		double wc   = Math.Max( 1e-4, Math.Min( 2.0 * Math.PI * Freq / fs, Math.PI - 1e-4 ) );
		double A    = Math.Pow( 10.0, GainDb / 40.0 );
		double sinW = Math.Sin( wc );
		double cosW = Math.Cos( wc );

		double b0, b1, b2, a0, a1, a2;

		switch ( Type )
		{
			case FilterType.LowShelf:
			{
				double alpha = sinW / 2.0 * Math.Sqrt( ( A + 1.0 / A ) * ( 1.0 / Q - 1.0 ) + 2.0 );
				double sq    = Math.Sqrt( A );

				b0 = A * ( ( A + 1 ) - ( A - 1 ) * cosW + 2 * sq * alpha );
				b1 = 2 * A * ( ( A - 1 ) - ( A + 1 ) * cosW );
				b2 = A * ( ( A + 1 ) - ( A - 1 ) * cosW - 2 * sq * alpha );
				a0 = ( A + 1 ) + ( A - 1 ) * cosW + 2 * sq * alpha;
				a1 = -2 * ( ( A - 1 ) + ( A + 1 ) * cosW );
				a2 = ( A + 1 ) + ( A - 1 ) * cosW - 2 * sq * alpha;
				break;
			}

			case FilterType.HighShelf:
			{
				double alpha = sinW / 2.0 * Math.Sqrt( ( A + 1.0 / A ) * ( 1.0 / Q - 1.0 ) + 2.0 );
				double sq    = Math.Sqrt( A );

				b0 = A * ( ( A + 1 ) + ( A - 1 ) * cosW + 2 * sq * alpha );
				b1 = -2 * A * ( ( A - 1 ) + ( A + 1 ) * cosW );
				b2 = A * ( ( A + 1 ) + ( A - 1 ) * cosW - 2 * sq * alpha );
				a0 = ( A + 1 ) - ( A - 1 ) * cosW + 2 * sq * alpha;
				a1 = 2 * ( ( A - 1 ) - ( A + 1 ) * cosW );
				a2 = ( A + 1 ) - ( A - 1 ) * cosW - 2 * sq * alpha;
				break;
			}

			default: // Peak
			{
				double alpha = sinW / ( 2.0 * Q );

				b0 = 1 + alpha * A;
				b1 = -2 * cosW;
				b2 = 1 - alpha * A;
				a0 = 1 + alpha / A;
				a1 = -2 * cosW;
				a2 = 1 - alpha / A;
				break;
			}
		}

		return [ b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0 ];
	}

	// Compute complex frequency response H(f) at the given frequency
	public Complex GetResponse( double freqHz, float fs )
	{
		var sos   = GetSos( fs );
		double w  = 2.0 * Math.PI * freqHz / fs;
		var z1inv = new Complex( Math.Cos( -w ), Math.Sin( -w ) );
		var z2inv = z1inv * z1inv;

		var num = new Complex( sos[ 0 ], 0 ) + sos[ 1 ] * z1inv + sos[ 2 ] * z2inv;
		var den = new Complex( 1.0,      0 ) + sos[ 4 ] * z1inv + sos[ 5 ] * z2inv;

		return num / den;
	}
}

// ── Direct Form II Transposed biquad IIR filter ────────────────────────────

internal sealed class BiquadFilter
{
	private readonly double _b0, _b1, _b2, _a1, _a2;
	private double _s1, _s2; // delay state

	public BiquadFilter( double[] sos )
	{
		// sos: [b0, b1, b2, 1.0, a1, a2]
		_b0 = sos[ 0 ]; _b1 = sos[ 1 ]; _b2 = sos[ 2 ];
		_a1 = sos[ 4 ]; _a2 = sos[ 5 ];
	}

	public float Process( float x )
	{
		double y = _b0 * x + _s1;
		_s1 = _b1 * x - _a1 * y + _s2;
		_s2 = _b2 * x - _a2 * y;
		return (float) y;
	}

	public void Reset() => _s1 = _s2 = 0.0;
}

// ── Real-time FFT EQ processor ─────────────────────────────────────────────

public class FftEqProcessor
{
	// MultimediaTimer fires every 2 ms → ~500 Hz actual call rate.
	// We use 500 Hz for filter design so label frequencies are accurate.
	public const float SampleRate = 500f;

	private const int    FftSize    = 256;     // ~0.51 s window at 500 Hz
	private const int    ChunkSize  = 17;      // FFT update every 17 samples (~29 Hz display)
	private const double EmaAlpha   = 0.20;

	public int SpectrumSize => FftSize / 2 + 1; // 129 bins, 0–250 Hz

	// Ring buffers — only touched by the worker thread
	private readonly float[] _rawRing = new float[ FftSize ];
	private readonly float[] _fxRing  = new float[ FftSize ];
	private int _ringHead  = 0;
	private int _chunkCount = 0;

	// Hanning window (pre-computed once)
	private readonly double[] _hanning = new double[ FftSize ];

	// Frequency axis (pre-computed once)
	public readonly double[] FreqBins = new double[ FftSize / 2 + 1 ];

	// Spectrum arrays — written by worker, read by UI under lock
	private readonly object _specLock = new();
	private readonly double[] _rawSpecDb = new double[ FftSize / 2 + 1 ];
	private readonly double[] _fxSpecDb  = new double[ FftSize / 2 + 1 ];

	// Active filter chain — volatile so worker sees UI writes immediately
	private volatile BiquadFilter[] _filters = [];

	// Snapshot of enabled bands for EQ curve rendering (UI thread only)
	private volatile EqBand[] _activeBands = [];

	public FftEqProcessor()
	{
		for ( int i = 0; i < FftSize; i++ )
			_hanning[ i ] = 0.5 * ( 1.0 - Math.Cos( 2.0 * Math.PI * i / ( FftSize - 1 ) ) );

		for ( int i = 0; i < SpectrumSize; i++ )
			FreqBins[ i ] = i * ( SampleRate / 2.0 ) / ( SpectrumSize - 1 );

		Array.Fill( _rawSpecDb, -80.0 );
		Array.Fill( _fxSpecDb,  -80.0 );
	}

	// Called from UI thread when the user edits EQ bands
	public void SetBands( IEnumerable<EqBand> bands )
	{
		var list = bands.ToList();

		_activeBands = [ .. list ];

		var newFilters = list
			.Where( b => b.Enabled )
			.Select( b => new BiquadFilter( b.GetSos( SampleRate ) ) )
			.ToArray();

		_filters = newFilters; // atomic reference swap
	}

	// Called from the worker thread at ~500 Hz.  Returns EQ-filtered torque.
	public float ProcessSample( float raw )
	{
		_rawRing[ _ringHead ] = raw;

		float fx = raw;
		foreach ( var f in _filters ) // volatile read → safe snapshot
			fx = f.Process( fx );

		_fxRing[ _ringHead ] = fx;
		_ringHead = ( _ringHead + 1 ) % FftSize;

		if ( ++_chunkCount >= ChunkSize )
		{
			_chunkCount = 0;
			ComputeSpectrum();
		}

		return fx;
	}

	private void ComputeSpectrum()
	{
		var rawData = new Complex[ FftSize ];
		var fxData  = new Complex[ FftSize ];

		double rawMean = 0, fxMean = 0;

		for ( int i = 0; i < FftSize; i++ )
		{
			rawMean += _rawRing[ i ];
			fxMean  += _fxRing[ i ];
		}

		rawMean /= FftSize;
		fxMean  /= FftSize;

		for ( int i = 0; i < FftSize; i++ )
		{
			int idx = ( _ringHead + i ) % FftSize;
			double w = _hanning[ i ];
			rawData[ i ] = new Complex( ( _rawRing[ idx ] - rawMean ) * w, 0 );
			fxData[ i ]  = new Complex( ( _fxRing[ idx ]  - fxMean  ) * w, 0 );
		}

		Fourier.Forward( rawData, FourierOptions.Default );
		Fourier.Forward( fxData,  FourierOptions.Default );

		lock ( _specLock )
		{
			double scale = FftSize / 2.0;

			for ( int i = 0; i < SpectrumSize; i++ )
			{
				double rawMag = rawData[ i ].Magnitude / scale;
				double fxMag  = fxData[ i ].Magnitude  / scale;

				double rawDb = rawMag > 1e-12 ? 20.0 * Math.Log10( rawMag ) : -80.0;
				double fxDb  = fxMag  > 1e-12 ? 20.0 * Math.Log10( fxMag  ) : -80.0;

				_rawSpecDb[ i ] = ( 1 - EmaAlpha ) * _rawSpecDb[ i ] + EmaAlpha * rawDb;
				_fxSpecDb[ i ]  = ( 1 - EmaAlpha ) * _fxSpecDb[ i ]  + EmaAlpha * fxDb;
			}
		}
	}

	// Called from UI thread (~30 Hz via DispatcherTimer)
	public void GetSpectrum( double[] rawDbOut, double[] fxDbOut )
	{
		lock ( _specLock )
		{
			Array.Copy( _rawSpecDb, rawDbOut, SpectrumSize );
			Array.Copy( _fxSpecDb,  fxDbOut,  SpectrumSize );
		}
	}

	// Returns a copy of active bands (safe for UI thread)
	public EqBand[] GetActiveBands() => [ .. _activeBands ];

	public void Reset()
	{
		_filters     = [];
		_activeBands = [];
		_chunkCount  = 0;

		lock ( _specLock )
		{
			Array.Fill( _rawSpecDb, -80.0 );
			Array.Fill( _fxSpecDb,  -80.0 );
		}
	}
}

// ── Top-level component registered in App ─────────────────────────────────

public class FftEq
{
	public FftEqProcessor Processor { get; } = new();

	private static List<EqBand> DefaultBands =>
	[
		new EqBand { Freq =  5f, GainDb = 0f, Q = 1.4f, Type = FilterType.LowShelf  },
		new EqBand { Freq = 20f, GainDb = 0f, Q = 1.4f, Type = FilterType.Peak      },
		new EqBand { Freq = 80f, GainDb = 0f, Q = 1.4f, Type = FilterType.HighShelf },
	];

	public void Initialize()
	{
		var app = App.Instance!;

		app.Logger.WriteLine( "[FftEq] Initialize >>>" );

		var bands = LoadBands();
		Processor.SetBands( bands );

		app.Logger.WriteLine( "[FftEq] <<< Initialize" );
	}

	// Called from the worker thread at ~500 Hz
	public float ProcessSample( float normalizedTorque )
	{
		if ( !DataContext.DataContext.Instance.Settings.FftEqEnabled )
			return normalizedTorque;

		return Processor.ProcessSample( normalizedTorque );
	}

	// Load band list from JSON setting (falls back to defaults)
	public List<EqBand> LoadBands()
	{
		try
		{
			var json  = DataContext.DataContext.Instance.Settings.FftEqBandsJson;
			var bands = JsonConvert.DeserializeObject<List<EqBand>>( json );

			if ( bands != null && bands.Count > 0 )
				return bands;
		}
		catch { /* fall through */ }

		return DefaultBands;
	}

	// Persist band list and push to processor
	public void SaveBands( List<EqBand> bands )
	{
		Processor.SetBands( bands );
		DataContext.DataContext.Instance.Settings.FftEqBandsJson =
			JsonConvert.SerializeObject( bands );
	}
}
